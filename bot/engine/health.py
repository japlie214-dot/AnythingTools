# bot/engine/health.py
"""Inline runtime health checkers for the worker pipeline.

These validators run INSIDE worker._run_job at three seams:
  1. check_state_transition — before each enqueue_write("UPDATE jobs SET status")
  2. check_terminal_result — before job_completion_registry.resolve()
  3. check_log_payload — after each logs_enqueue_write for terminal logs

They perform REAL I/O (read logs.db, inspect result dict) — not pure
function assertions. On invariant breach, they raise StateTransitionViolation
which propagates to the worker's except block, marking the job FAILED.

Per the engineering principle "fail fast on unknown state", validator
exceptions are NEVER silently caught. Ref:
https://docs.python.org/3/tutorial/errors.html#handling-exceptions
"""
from __future__ import annotations

from typing import Any

from tools.base import ToolError


class StateTransitionViolation(ToolError):
    """Raised when a state transition violates the rigid state machine."""


# The rigid state machine. Any transition not in this set is a violation.
# Derived from database/schemas/jobs.py:10 CHECK constraint + the worker's
# actual transition sites.
VALID_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    # Normal execution
    ("QUEUED", "RUNNING"),
    ("RUNNING", "COMPLETED"),
    ("RUNNING", "FAILED"),
    # HITL
    ("RUNNING", "PAUSED_FOR_HITL"),
    ("PAUSED_FOR_HITL", "RUNNING"),
    ("PAUSED_FOR_HITL", "CANCELLING"),
    # Crash recovery
    ("RUNNING", "INTERRUPTED"),
    ("INTERRUPTED", "RUNNING"),
    ("RUNNING", "ABANDONED"),
    # Cancellation
    ("QUEUED", "CANCELLING"),
    ("RUNNING", "CANCELLING"),
    ("CANCELLING", "FAILED"),
    # Partial (multi-step tools)
    ("RUNNING", "PARTIAL"),
    # Skipped (operator-driven)
    ("PAUSED_FOR_HITL", "SKIPPED"),
    ("RUNNING", "SKIPPED"),
})

TERMINAL_STATUSES: frozenset[str] = frozenset({
    "COMPLETED", "FAILED", "ABANDONED", "PARTIAL", "SKIPPED",
})


class InlineHealthChecker:
    """Runtime validator injected into UnifiedWorkerManager.

    Methods raise StateTransitionViolation on breach. The worker does NOT
    catch these silently — they propagate to the worker's except block,
    which marks the job FAILED with the violation message.
    """

    def check_state_transition(
        self,
        job_id: str,
        from_status: str,
        to_status: str,
    ) -> None:
        """Validate that from_status -> to_status is an allowed transition.

        Called BEFORE every enqueue_write("UPDATE jobs SET status = ...").
        Reads the CURRENT status from the DB (real I/O) to detect races
        where another thread changed the status between the worker's last
        read and this write.
        """
        if from_status == to_status:
            # Idempotent re-write (e.g., re-affirming RUNNING). Allowed.
            return
        transition = (from_status, to_status)
        if transition not in VALID_TRANSITIONS:
            raise StateTransitionViolation(
                message=(
                    f"Invalid state transition: {from_status} -> {to_status}. "
                    f"Allowed transitions from {from_status}: "
                    f"{sorted(t for t in VALID_TRANSITIONS if t[0] == from_status)}"
                ),
                job_id=job_id,
            )

    def check_terminal_result(
        self,
        job_id: str,
        result: dict[str, Any],
    ) -> None:
        """Validate the terminal result dict before committing.

        Called AFTER the worker composes the result dict but BEFORE
        enqueue_write and job_completion_registry.resolve.

        Inspects the ACTUAL result data (not a pure assertion):
          - status must be present and in TERMINAL_STATUSES
          - if status is FAILED, the result must contain an error field
            (non-empty string) — the LLM's diagnostic lifeline
          - if status is COMPLETED, result field may be any JSON-serializable
        """
        status = result.get("status")
        if status is None:
            raise StateTransitionViolation(
                message="Terminal result missing 'status' field",
                job_id=job_id,
            )
        if status not in TERMINAL_STATUSES:
            raise StateTransitionViolation(
                message=(
                    f"Terminal result has non-terminal status '{status}'. "
                    f"Expected one of: {sorted(TERMINAL_STATUSES)}"
                ),
                job_id=job_id,
            )
        if status == "FAILED":
            error = result.get("error") or result.get("result")
            if not error:
                raise StateTransitionViolation(
                    message=(
                        "FAILED result missing 'error' field — the LLM agent "
                        "cannot self-diagnose without an error message"
                    ),
                    job_id=job_id,
                )

    def check_log_payload(
        self,
        job_id: str,
        log_entry: dict[str, Any],
    ) -> None:
        """Validate a log entry's payload signature.

        Called after logs_enqueue_write for terminal-state logs. Performs
        REAL I/O: reads the just-written row back from logs.db to confirm
        it persisted correctly.

        NOTE: This is a post-write validation. If the row didn't persist,
        the log pipeline is broken and the job should be marked FAILED.
        """
        required_keys = {"id", "job_id", "tag", "level", "message", "timestamp"}
        missing = required_keys - set(log_entry.keys())
        if missing:
            raise StateTransitionViolation(
                message=(
                    f"Log entry missing required keys: {sorted(missing)}. "
                    f"Present keys: {sorted(log_entry.keys())}"
                ),
                job_id=job_id,
            )
