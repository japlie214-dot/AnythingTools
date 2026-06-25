# utils/observability/accumulator.py
"""ActivityAccumulator — the core of the Activity-Driven Observability framework.

One accumulator per job (when capture_lineage=true). Records named activities
with their inputs, outputs, status, and error. Finalizes into a LineageReport
that the synthetic tracer asserts against.

Thread-safety: the activities list is guarded by a threading.Lock, mirroring
the pattern in bot/engine/completion_registry.py. Per
https://docs.python.org/3/library/threading.html#threading.Lock
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from utils.observability.masking import serialize_safe, DEFAULT_MAX_CHARS

# --- Pydantic v2 models for the lineage report ---
# Per https://docs.pydantic.dev/latest/concepts/models/


class ActivityRecord(BaseModel):
    """A single activity's record in the lineage.

    Per Developer Contract in utils/observability/__init__.py §4.3.a:
    - activity_name: verb-phrase (e.g., "Validate StockFinancialsInput")
    - status: PASSED or FAILED
    - inputs: named-parameter-bound inputs, truncated/masked
    - outputs: the return value, truncated/masked; None on failure
    - error: the failure message; None on success (NEVER truncated — diagnostic lifeline)
    """
    activity_name: str
    status: Literal["PASSED", "FAILED"]
    inputs: Optional[Any] = None
    outputs: Optional[Any] = None
    error: Optional[str] = None
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    ended_at: Optional[str] = None
    duration_ms: float = 0.0


class LineageReport(BaseModel):
    """The verification artifact. Per Developer Contract in utils/observability/__init__.py §4.3.e:

    - summary: overall status + counts
    - lineage: ordered list of ActivityRecords
    - business_response_snapshot: the business response (null on failure)
    """
    summary: dict
    lineage: list[ActivityRecord]
    business_response_snapshot: Optional[Any] = None

    class Config:
        # Allow arbitrary types in case the business response contains
        # non-Pydantic objects (they're already serialized_safe by the time
        # they reach here).
        arbitrary_types_allowed = True


class ActivityAccumulator:
    """Per-job accumulator. Created in bot/engine/worker.py::_run_job when
    capture_lineage=true. Bound to _current_accumulator ContextVar for
    propagation through the worker's call graph.

    The accumulator NEVER raises into tool code. All internal errors are
    caught and logged. The @activity decorator re-raises tool exceptions
    (the tool's contract is unchanged) but records FAILED before re-raising.
    """

    def __init__(
        self,
        job_id: str,
        tool_name: str,
        *,
        max_activities: int = 1000,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self.job_id = job_id
        self.tool_name = tool_name
        self.capture_lineage = True  # Always true when an accumulator exists.
        self.max_activities = max_activities
        self.max_chars = max_chars
        self._activities: list[ActivityRecord] = []
        self._lock = threading.Lock()
        self._dropped_count = 0
        self._started_at = datetime.now(timezone.utc).isoformat()

    def record(
        self,
        name: str,
        inputs: Any,
        *,
        outputs: Any = None,
        error: Optional[str] = None,
        started_at: Optional[str] = None,
        ended_at: Optional[str] = None,
        duration_ms: float = 0.0,
    ) -> None:
        """Record an activity. Called by the @activity decorator.

        Inputs and outputs are serialized_safe (truncated + masked) BEFORE
        storage to bound peak memory. The error field is NEVER truncated —
        it is the diagnostic lifeline (per tools/base.py:30 ToolError contract).

        This method NEVER raises. If serialization fails, the activity is
        recorded with a placeholder.
        """
        try:
            serialized_inputs = serialize_safe(inputs, max_chars=self.max_chars)
        except Exception:
            serialized_inputs = "<inputs-serialization-failed>"

        try:
            serialized_outputs = serialize_safe(outputs, max_chars=self.max_chars)
        except Exception:
            serialized_outputs = "<outputs-serialization-failed>"

        status = "FAILED" if error is not None else "PASSED"

        record = ActivityRecord(
            activity_name=name,
            status=status,
            inputs=serialized_inputs,
            outputs=serialized_outputs,
            error=error,
            started_at=started_at or datetime.now(timezone.utc).isoformat(),
            ended_at=ended_at or datetime.now(timezone.utc).isoformat(),
            duration_ms=duration_ms,
        )

        with self._lock:
            if len(self._activities) >= self.max_activities:
                self._dropped_count += 1
                return
            self._activities.append(record)

    def record_failure(
        self,
        name: str,
        inputs: Any,
        error: str,
    ) -> None:
        """Explicitly register a failure for activities that catch exceptions
        internally and return a default value.

        Per Developer Contract in utils/observability/__init__.py §4.3.b: "If an activity catches an exception
        internally, it must either re-raise it OR register the failure status
        cleanly to the accumulator."

        This method is for the "register cleanly" path. It records a FAILED
        activity with the given error message. The activity's outputs are
        set to None (since the activity caught the error and may have returned
        a default — that default is NOT recorded as a successful output).
        """
        self.record(name, inputs, outputs=None, error=error)

    def finalize(self, business_response: Any = None) -> LineageReport:
        """Build the final LineageReport. Called in _run_job after the tool
        completes (success or failure).

        The business_response is the tool's return value (already parsed by
        the worker). It is included as business_response_snapshot per the
        Developer Contract in utils/observability/__init__.py §4.3.e LineageReport shape.

        This method NEVER raises.
        """
        with self._lock:
            activities_snapshot = list(self._activities)
            dropped = self._dropped_count

        total = len(activities_snapshot)
        passed = sum(1 for a in activities_snapshot if a.status == "PASSED")
        failed = sum(1 for a in activities_snapshot if a.status == "FAILED")
        overall_status = "FAILED" if failed > 0 else "PASSED"

        # Serialize the business response snapshot.
        try:
            snapshot = serialize_safe(business_response, max_chars=self.max_chars)
        except Exception:
            snapshot = "<business-response-serialization-failed>"

        return LineageReport(
            summary={
                "status": overall_status,
                "total_activities_executed": total,
                "passed": passed,
                "failed": failed,
                "dropped_count": dropped,
                "job_id": self.job_id,
                "tool_name": self.tool_name,
                "started_at": self._started_at,
                "ended_at": datetime.now(timezone.utc).isoformat(),
            },
            lineage=activities_snapshot,
            business_response_snapshot=snapshot,
        )

    def is_active(self) -> bool:
        """Always True when an accumulator exists (it's only created when
        capture_lineage=true)."""
        return self.capture_lineage
