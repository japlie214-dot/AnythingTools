# utils/observability/context.py
"""ContextVar-based propagation for the ActivityAccumulator.

The accumulator is bound in bot/engine/worker.py::_run_job and propagates
through:
- asyncio.run() — Runner.__init__ calls contextvars.copy_context() per
  https://docs.python.org/3/library/asyncio-taskrunner.html#asyncio.Runner
- to_thread_with_context() — calls contextvars.copy_context() per
  utils/context_helpers.py:56
- spawn_thread_with_context() — calls contextvars.copy_context() per
  utils/context_helpers.py:33

It does NOT cross the API-handler → polling-thread boundary (plain
threading.Thread at worker.py:63 does not copy context). The capture_lineage
boolean crosses that boundary via the jobs.args_json column.
"""
from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from utils.observability.accumulator import ActivityAccumulator

# The ContextVar. Default None means "no accumulator active" (Standard Mode).
# Per https://docs.python.org/3/library/contextvars.html#contextvars.ContextVar
_current_accumulator: contextvars.ContextVar[Optional["ActivityAccumulator"]] = (
    contextvars.ContextVar("_current_accumulator", default=None)
)


def get_current_accumulator() -> Optional["ActivityAccumulator"]:
    """Return the accumulator bound to the current context, or None.

    Called by the @activity decorator. When None, the decorator is a no-op
    pass-through (Standard Mode — zero overhead).
    """
    return _current_accumulator.get()


def bind_accumulator(acc: "ActivityAccumulator") -> contextvars.Token:
    """Bind an accumulator to the current context.

    Returns a Token that MUST be passed to unbind_accumulator() in a finally
    block. Per https://docs.python.org/3/library/contextvars.html#contextvars.ContextVar.reset
    failing to reset the token leaks the accumulator across job boundaries.
    """
    return _current_accumulator.set(acc)


def unbind_accumulator(token: contextvars.Token) -> None:
    """Reset the ContextVar to its previous value.

    Safe to call even if the token was already reset (ContextVar.reset on an
    already-used token raises ValueError — caught here for defensive use in
    finally blocks).
    """
    try:
        _current_accumulator.reset(token)
    except (ValueError, LookupError):
        pass
