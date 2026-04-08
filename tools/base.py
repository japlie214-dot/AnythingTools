# tools/base.py
"""Base classes for tool implementations.

Each tool must inherit from ``BaseTool`` and implement the ``run`` coroutine.
The ``run`` method receives a telemetry callback that accepts a ``StatusUpdate``.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from bot.telemetry import StatusUpdate


TelemetryCallback = Callable[[StatusUpdate], Awaitable[None]]


@dataclass
class ToolResult:
    """Structured return type for ``BaseTool.execute``.

    attachment_paths — list of absolute file paths produced by the tool
                       (e.g. one path per image slice). None when the tool
                       produces no file output.
    event_id         — shared ULID for all paths in attachment_paths when
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

    Sub‑classes should set ``name`` and implement ``run``.
    """

    name: str

    def is_resumable(self, args: dict[str, Any]) -> bool:
        """Return True if this tool supports mid-run resume for the given args."""
        return False

    def status(self, message: str, status: str = "RUNNING") -> StatusUpdate:
        """Convenience helper to create a ``StatusUpdate`` for this tool."""
        from datetime import datetime, timezone

        return StatusUpdate(
            timestamp=datetime.now(timezone.utc).strftime("%H:%M:%S"),
            tool_name=self.name,
            message=message,
            status=status,
        )

    @abc.abstractmethod
    async def run(self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs) -> str:
        """Execute tool logic and emit telemetry via the provided callback.

        MANDATORY DEVELOPER CONTRACT:
        The returned string must be a complete, human-readable text summary that
        stands alone without any attached files.  Attachment files (images, PDFs,
        documents) are subject to FIFO eviction by the orchestrator at any time;
        the text return value is the only guaranteed-persistent output.

        Additional keyword arguments may include:
        - dry_run: bool    — When True, skip external side effects.
        - session_id: str  — Unique identifier for the current session.
        - chat_id: int     — Telegram chat identifier.
        """
        raise NotImplementedError

    async def execute(
        self,
        args: dict[str, Any],
        telemetry: TelemetryCallback,
        **kwargs,
    ) -> ToolResult:
        """Wrap ``run()`` with isolated log capture and automatic failure diagnosis.

        On success  → ToolResult(output=<run result>, success=True)
        On failure  → ToolResult(output=<diagnosis or traceback>, success=False)

        The ContextVar ``_tool_log_buffer`` is active only for the duration
        of this call and is unconditionally reset in ``finally``.
        """
        # ── Lazy imports to break circular chains through SourceContextManager ──
        import json as _json
        import traceback as _traceback
        import config as _exec_config   # lazy import consistent with existing pattern
        from utils.logger.state import _tool_log_buffer, _current_job_id  # direct singleton reference
        from utils.logger import get_dual_logger

        from utils.source_context import SourceContextManager
        from clients.llm import get_llm_client, LLMRequest
        from tools.logger_agent.logger_prompts import (
            ERROR_HEADER,
            LOGGER_AGENT_SYSTEM_PROMPT,
            trim_log_buffer,
        )

        # Register current job id (compatibility layer). Accepts a `job_id` kwarg
        # when the caller (worker) provides it; default is None which keeps
        # legacy behavior unchanged.
        job_id = kwargs.get("job_id") if kwargs is not None else None
        token_job = _current_job_id.set(job_id)

        token = _tool_log_buffer.set([])
        try:
            # ── ADD: Black Box boundary log for tool invocation input ──
            _base_log = get_dual_logger(__name__)
            _base_log.dual_log(
                tag=f"Tool:{self.name}:Execute",
                message=f"Starting execution of {self.name}",
                payload={"args": args, "kwargs": kwargs},
            )
            # ── END ADD ──
            result = await self.run(args, telemetry, **kwargs)
            return ToolResult(output=result or "", success=True, attachment_paths=None, event_id=None, diagnosis=None)

        except Exception:
            # ── Immediate feedback (best-effort, must not derail diagnosis) ─
            try:
                await telemetry(self.status(
                    "Tool failed. Diagnosing root cause...", "RUNNING",
                ))
            except Exception:
                pass

            raw_tb = _traceback.format_exc()

            try:
                source_text = SourceContextManager.get_tool_sources(self.name)
                buf = _tool_log_buffer.get() or []
                trimmed_logs = trim_log_buffer(
                    buf,
                    max_chars=getattr(_exec_config, 'LOGGER_AGENT_MAX_CONTEXT', 100_000),
                )

                from utils.text_processing import escape_prompt_separators
                user_content = (
                    f"### Tool\n{self.name}\n\n"
                    f"### Traceback\n{escape_prompt_separators(raw_tb)}\n\n"
                    f"### Logs\n{escape_prompt_separators(trimmed_logs)}\n\n"
                    f"### Source Code\n{escape_prompt_separators(source_text)}\n###\n{{"
                )

                llm = get_llm_client(provider_type="azure")
                response = await llm.complete_chat(LLMRequest(
                    messages=[
                        {"role": "system", "content": LOGGER_AGENT_SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    model="gpt-5.4-mini",
                    response_format={"type": "json_object"},
                ))

                parsed = _json.loads(response.content)
                user_msg = parsed.get(
                    "user_message",
                    f"{ERROR_HEADER}\nAn unknown error occurred.",
                )

                # Enforce header even if the LLM omitted it
                if not user_msg.startswith(ERROR_HEADER):
                    user_msg = f"{ERROR_HEADER}\n{user_msg}"

                return ToolResult(
                    output=user_msg, success=False, attachment_paths=None, event_id=None, diagnosis=parsed,
                )

            except Exception:
                # ── Hard fallback: raw traceback prefixed with header ─
                fallback = f"{ERROR_HEADER}\n\n{raw_tb}"
                return ToolResult(
                    output=fallback, success=False, attachment_paths=None, event_id=None, diagnosis=None,
                )

        finally:
            # Flush buffered tool logs into the persistent job_logs table so the
            # caller (worker) and external agents can observe per-job progress.
            try:
                from utils.logger.core import flush_tool_buffer_to_job_logs
                current_job_id = _current_job_id.get()
                buf = _tool_log_buffer.get() or []
                flush_tool_buffer_to_job_logs(current_job_id, buf)
            except Exception:
                pass

            _tool_log_buffer.reset(token)
            _current_job_id.reset(token_job)
