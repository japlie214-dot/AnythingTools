# utils/context_helpers.py
"""Helpers to spawn threads / run blocking functions while propagating contextvars.

Usage:
- Use spawn_thread_with_context(func, args=(...), kwargs={}, name=None, daemon=True) to start a daemon thread
  whose execution context is a copy of the caller's Context (so _current_job_id/_tool_log_buffer are preserved).
- Use to_thread_with_context(func, *args, **kwargs) as an async replacement for asyncio.to_thread that also
  propagates the current contextvars into the worker thread.

These helpers are intentionally tiny and dependency-free so they can be imported early.
"""

from __future__ import annotations

import contextvars
import threading
import asyncio
from typing import Callable, Any, Tuple, Dict


def spawn_thread_with_context(
    func: Callable[..., Any],
    args: Tuple = (),
    kwargs: Dict | None = None,
    name: str | None = None,
    daemon: bool = True,
) -> threading.Thread:
    """Spawn a daemon Thread and copy the current context into it.

    The returned Thread is already started. Exceptions inside the thread are
    printed to stderr (best-effort) to avoid silent failures.
    """
    ctx = contextvars.copy_context()
    if kwargs is None:
        kwargs = {}

    def _runner() -> None:
        try:
            ctx.run(func, *args, **kwargs)
        except Exception:
            # Minimal fallback: print stack so background failures are visible in logs.
            import traceback

            traceback.print_exc()

    t = threading.Thread(target=_runner, name=name, daemon=daemon)
    t.start()
    return t


async def to_thread_with_context(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Async helper that runs `func(*args, **kwargs)` in a thread while propagating
    the current contextvars to that thread. Use this in place of ``asyncio.to_thread``
    when the called function needs access to contextvars (e.g., _current_job_id).
    """
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()

    def _run() -> Any:
        return ctx.run(func, *args, **(kwargs or {}))

    return await loop.run_in_executor(None, _run)


def run_in_context(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a function in the current context (utility for synchronous callers wishing to
    serialize access to contextvars within a controlled block)."""
    return func(*args, **kwargs)
