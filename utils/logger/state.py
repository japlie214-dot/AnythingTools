# utils/logger/state.py
"""
ContextVar-backed logger state used by AnythingTools.
This module is a compatibility bridge so existing code can continue to call
`dual_log(...)` without changing signatures. It exposes the ContextVar
singletons used across the codebase.
"""

import contextvars
import threading

_logger_ready = threading.Event()

try:
    import config as _log_config  # read-only; may be None during early init
except Exception:
    _log_config = None  # type: ignore[assignment]

# ContextVar: None when inactive, list[dict] when a tool is executing.
# tools/base.py imports this symbol directly from this module to guarantee
# both sides reference the exact same ContextVar singleton.
_tool_log_buffer: contextvars.ContextVar[list[dict] | None] = (
    contextvars.ContextVar("_tool_log_buffer", default=None)
)

# Compatibility ContextVar for the currently executing job id.
# Deeply nested utilities call `dual_log(...)` without a job id; this ContextVar
# allows the logger to attach the in-progress job id to any buffered entries.
_current_job_id: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("_current_job_id", default=None)
)


def get_current_job_id() -> str | None:
    """Return the current job id stored in the ContextVar (may be None)."""
    return _current_job_id.get()
