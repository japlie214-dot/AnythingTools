# tools/base.py
"""Base classes for tool implementations.

Each tool must inherit from ``BaseTool`` and implement the ``run`` coroutine.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolResult:
    """Structured return type for ``BaseTool.execute``.

    attachment_paths  list of absolute file paths produced by the tool
                       (e.g. one path per image slice). None when the tool
                       produces no file output.
    event_id          shared ULID for all paths in attachment_paths when
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

    Subclasses should set ``name`` and implement ``run``.
    """

    name: str

    def is_resumable(self, args: dict[str, Any]) -> bool:
        """Return True if this tool supports mid-run resume for the given args."""
        return False

    def status(self, message: str, status: str = "RUNNING") -> dict:
        """Convenience helper to create a status update for this tool."""
        from datetime import datetime, timezone
        return {
            "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "tool_name": self.name,
            "message": message,
            "status": status,
        }

    @abc.abstractmethod
    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        """Execute tool logic and emit telemetry via the provided callback.

        MANDATORY DEVELOPER CONTRACT:
        The returned string must be a complete, human-readable text summary that
        stands alone without any attached files.  Attachment files (images, PDFs,
        documents) are subject to FIFO eviction by the orchestrator at any time;
        the text return value is the only guaranteed-persistent output.

        Additional keyword arguments may include:
        - dry_run: bool     When True, skip external side effects.
        - session_id: str   Unique identifier for the current session.
        - chat_id: int      Telegram chat identifier.
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
