# tools/base.py
"""Base classes for tool implementations.

Each tool must inherit from ``BaseTool`` and implement:
  - ``run(args, telemetry, **kwargs) -> str``  — the tool's execution logic
  - ``health_check_payload()``                 — returns inputs for E2E health checks

The ``run`` method must return a plain string (markdown). The previous
``_callback_format: structured`` dict pattern has been removed in favor
of SSE-streamed events. Tools that need to emit intermediate progress
can call ``telemetry(self.status(message, state))`` which the SSE broker
intercepts and forwards to subscribers.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Optional
from enum import Enum
from dataclasses import dataclass, field

class ToolError(Exception):
    """Base for all tool-raised errors. Carries full diagnostic context.

    Tools MUST raise this (or a subclass) on failure instead of returning
    a markdown error string. The worker catches it, marks the job FAILED,
    and propagates the full message to the LLM agent via the sync API response.

    The message is NEVER truncated — it is the LLM's diagnostic lifeline.
    Ref: https://docs.python.org/3/tutorial/errors.html#raising-exceptions
    """
    def __init__(
        self,
        message: str,
        *,
        tool_name: str | None = None,
        job_id: str | None = None,
        cause: Exception | None = None,
        next_steps: str | None = None,
    ) -> None:
        # Compose a fully self-contained message. Length is not a concern —
        # the requirement explicitly states "exception message should contain
        # all information needed, length isn't an issue".
        parts = [message]
        if tool_name:
            parts.append(f"tool_name={tool_name}")
        if job_id:
            parts.append(f"job_id={job_id}")
        if next_steps:
            parts.append(f"next_steps={next_steps}")
        if cause:
            # Include the cause's repr AND traceback so the LLM can self-diagnose
            # without querying logs.db. Per Python exception chaining docs:
            # https://docs.python.org/3/tutorial/errors.html#exception-chaining
            import traceback as _tb
            cause_tb = "".join(_tb.format_exception(type(cause), cause, cause.__traceback__))
            parts.append(f"cause_type={type(cause).__name__}")
            parts.append(f"cause_message={cause}")
            parts.append(f"cause_traceback={cause_tb}")
        super().__init__(" | ".join(parts))
        self.tool_name = tool_name
        self.job_id = job_id
        self.cause = cause
        self.next_steps = next_steps


class ToolValidationError(ToolError):
    """Raised when tool input fails validation. Maps to HTTP 422 semantics."""


class ToolExecutionError(ToolError):
    """Raised when a tool fails at runtime. Maps to job status FAILED."""


class FailureSeverity(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    CONFIGURATION = "configuration"
    DATA = "data"


@dataclass
class StatusOverride:
    description: str
    severity: FailureSeverity = FailureSeverity.TRANSIENT
    next_steps: str = "No action required."
    rerunnable: bool = False
    diagnostics: Optional[list[str]] = None

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "severity": self.severity.value,
            "next_steps": self.next_steps,
            "rerunnable": self.rerunnable,
            "diagnostics": self.diagnostics or []
        }


@dataclass
class HealthCheckPayload:
    """Describes the inputs for a tool's health check.

    A health check runs the tool end-to-end against the staging database
    (DATABASE_STAGING_ENABLED=true). Both happy and error paths are
    exercised to validate the tool's full execution surface.

    Attributes:
        happy_path_args: Input args that should result in a successful
            execution (status=COMPLETED). This validates the tool's
            primary workflow.
        error_path_args: Input args that should trigger a controlled
            failure (status=FAILED). This validates the tool's error
            handling and that failures are surfaced (not silently
            swallowed).
        expected_happy_status: The expected terminal status for the
            happy path. Usually "COMPLETED" but some tools (e.g.,
            publisher with an empty batch) may legitimately end in
            "PARTIAL" or "FAILED".
        expected_error_status: The expected terminal status for the
            error path. Usually "FAILED".
        timeout_seconds: Override for HEALTH_CHECK_TIMEOUT_SECONDS.
            Set higher for browser-based tools (scraper).
    """
    happy_path_args: dict[str, Any]
    error_path_args: dict[str, Any]
    expected_happy_status: str = "COMPLETED"
    expected_error_status: str = "FAILED"
    timeout_seconds: int | None = None


@dataclass
class ResumeReport:
    """Rich detail report generated by a tool upon resume validation."""
    tool_name: str
    resumable: bool
    items_completed: int
    items_pending: int
    message: str
    details: dict | None = None


class BaseResumeHandler(abc.ABC):
    """Abstract base class for tool-specific resume logic."""

    def __init__(self, job_id: str, args: dict[str, Any]):
        self.job_id = job_id
        self.args = args

    @abc.abstractmethod
    def check_resume_state(self) -> ResumeReport:
        """Validate state and return a detailed resume report."""
        raise NotImplementedError


@dataclass
class ToolResult:
    """Structured return type for ``BaseTool.execute``.

    attachment_paths - list of absolute file paths produced by the tool
                       (e.g. one path per image slice). None when the tool
                       produces no file output.
    event_id         - shared ULID for all paths in attachment_paths when
                       they originate from a single capture_and_optimize()
                       call. None for tools that do not capture images.
    """
    output: str
    success: bool
    attachment_paths: list[str] | None = None
    event_id: str | None = None
    diagnosis: dict | None = None


class BaseTool(abc.ABC):
    """Abstract base class for all tools.

    Subclasses must set ``name`` and implement both ``run`` and
    ``health_check_payload``.
    """

    name: str

    def is_resumable(self, args: dict[str, Any]) -> bool:
        """Return True if this tool supports mid-run resume for the given args."""
        return False

    def status(self, message: str, status: str = "RUNNING", payload: dict | None = None) -> dict:
        """Convenience helper to create a status update for this tool.

        The SSE broker intercepts telemetry calls and forwards them as
        ``tool.progress`` events to subscribers.
        """
        from datetime import datetime, timezone
        result = {
            "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "tool_name": self.name,
            "message": message,
            "status": status,
        }
        if payload is not None:
            result["payload"] = payload
        return result

    @abc.abstractmethod
    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        """Execute tool logic and emit telemetry via the provided callback.

        MANDATORY DEVELOPER CONTRACT:
        The returned string must be a complete, human-readable markdown
        summary that stands alone without any attached files. Attachment
        files (images, PDFs, documents) are subject to FIFO eviction by
        the orchestrator at any time; the text return value is the only
        guaranteed-persistent output.

        Do NOT return JSON with ``_callback_format: structured`` — that
        pattern has been removed. Return plain markdown text.

        Additional keyword arguments may include:
        - dry_run: bool    - When True, skip external side effects.
        - session_id: str  - Unique identifier for the current session.
        - job_id: str      - The job's ULID for log correlation.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def health_check_payload(self) -> HealthCheckPayload:
        """Return inputs for end-to-end health checking.

        The health check enqueues a real job using these args against
        the staging database. Both happy and error paths are exercised.

        Tools that require external resources (EDGAR, browser, Telegram)
        must ensure their health-check payloads use safe, non-destructive
        inputs (e.g., a well-known stable ticker like AAPL, a test batch
        ID, a dry-run flag).
        """
        raise NotImplementedError

    async def execute(
        self,
        args: dict[str, Any],
        telemetry: Any,
        **kwargs,
    ) -> ToolResult:
        """Execute tool logic. Exceptions are caught by bot/engine/tool_runner.py."""
        from utils.logger.state import _tool_log_buffer, _current_job_id
        from utils.logger import get_dual_logger
        self._last_artifacts = None  # Ensure pristine state per execution
        job_id = kwargs.get("job_id") if kwargs is not None else None
        token_job = _current_job_id.set(job_id)
        token = _tool_log_buffer.set([])
        try:
            _base_log = get_dual_logger(__name__)
            _base_log.dual_log(
                tag=f"Tool:{self.name}:Execute",
                message=f"Starting execution of {self.name}",
                payload={"args": args, "kwargs": kwargs},
            )
            result = await self.run(args, telemetry, **kwargs)
            _base_log.dual_log(
                tag=f"Tool:{self.name}:Complete",
                message=f"Execution of {self.name} completed",
                payload={"job_id": job_id, "success": True, "output_len": len(result) if result else 0}
            )
            return ToolResult(output=result or "", success=True)
        finally:
            try:
                from utils.logger.core import flush_tool_buffer_to_job_logs
                current_job_id = _current_job_id.get()
                buf = _tool_log_buffer.get() or []
                flush_tool_buffer_to_job_logs(current_job_id, buf)
            except Exception:
                pass
            _tool_log_buffer.reset(token)
            _current_job_id.reset(token_job)
