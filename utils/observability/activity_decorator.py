# utils/observability/activity_decorator.py
"""The @activity decorator — wraps functions to auto-record their execution.

Per convention §4.3.b: the wrapper does three things:
1. Binds the activity's declared inputs by name.
2. Calls the underlying logic.
3. On success records the output; on failure records the error and re-raises.

The wrapper NEVER swallows exceptions — it records and re-raises, so the
entry point's own error handling decides what to do with the failure.
"""
from __future__ import annotations

import functools
import inspect
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional, TypeVar

from utils.observability.context import get_current_accumulator

F = TypeVar("F", bound=Callable[..., Any])


def _extract_inputs(func: Callable, args: tuple, kwargs: dict) -> dict:
    """Extract named inputs from args/kwargs, excluding 'self' and 'accumulator'.

    Uses inspect.signature.bind to map positional args to parameter names.
    Per https://docs.python.org/3/library/inspect.html#inspect.Signature.bind
    """
    try:
        sig = inspect.signature(func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        result = {}
        for name, value in bound.arguments.items():
            if name == "self":
                result[name] = f"<{type(value).__name__}>"
            elif name == "accumulator":
                continue  # Never record the accumulator itself.
            else:
                result[name] = value
        return result
    except Exception:
        # If signature binding fails (e.g., *args, **kwargs), fall back to
        # recording positional args as a list.
        return {"_args": list(args), "_kwargs": dict(kwargs)}


def activity(name: Optional[str] = None) -> Callable[[F], F]:
    """Decorator that records the wrapped function as an ActivityRecord.

    Args:
        name: The activity name (verb-phrase). If None, uses func.__name__.

    Usage:
        @activity("Validate StockFinancialsInput")
        def _validate_input(self, args, job_id):
            ...

    Behavior:
        - If no accumulator is active (Standard Mode): zero-overhead pass-through.
        - If accumulator is active (Observability Mode):
            - Records inputs (named, excluding self/accumulator).
            - On success: records outputs, status=PASSED.
            - On exception: records error (NEVER truncated), status=FAILED, then RE-RAISES.
        - Sync and async functions supported (dispatch via inspect.iscoroutinefunction).

    Per convention Developer Rule #2: "Pass the accumulator forward." The
    decorator reads the accumulator from the ContextVar, not from kwargs.
    Per convention Developer Rule #3: "The wrapper never swallows exceptions."
    """
    def decorator(func: F) -> F:
        activity_name = name or func.__name__

        if inspect.iscoroutinefunction(func):
            # Async path.
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                accumulator = get_current_accumulator()
                if accumulator is None or not accumulator.is_active():
                    # Standard Mode: zero overhead.
                    return await func(*args, **kwargs)

                inputs = _extract_inputs(func, args, kwargs)
                started_at = datetime.now(timezone.utc).isoformat()
                start = time.monotonic()
                try:
                    result = await func(*args, **kwargs)
                    duration_ms = (time.monotonic() - start) * 1000
                    accumulator.record(
                        activity_name,
                        inputs=inputs,
                        outputs=result,
                        error=None,
                        started_at=started_at,
                        ended_at=datetime.now(timezone.utc).isoformat(),
                        duration_ms=duration_ms,
                    )
                    return result
                except Exception as e:
                    duration_ms = (time.monotonic() - start) * 1000
                    # Error message is NEVER truncated — it is the diagnostic
                    # lifeline. Per tools/base.py:30 ToolError contract.
                    accumulator.record(
                        activity_name,
                        inputs=inputs,
                        outputs=None,
                        error=str(e),
                        started_at=started_at,
                        ended_at=datetime.now(timezone.utc).isoformat(),
                        duration_ms=duration_ms,
                    )
                    raise  # RE-RAISE — never swallow.

            return async_wrapper  # type: ignore

        else:
            # Sync path.
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                accumulator = get_current_accumulator()
                if accumulator is None or not accumulator.is_active():
                    # Standard Mode: zero overhead.
                    return func(*args, **kwargs)

                inputs = _extract_inputs(func, args, kwargs)
                started_at = datetime.now(timezone.utc).isoformat()
                start = time.monotonic()
                try:
                    result = func(*args, **kwargs)
                    duration_ms = (time.monotonic() - start) * 1000
                    accumulator.record(
                        activity_name,
                        inputs=inputs,
                        outputs=result,
                        error=None,
                        started_at=started_at,
                        ended_at=datetime.now(timezone.utc).isoformat(),
                        duration_ms=duration_ms,
                    )
                    return result
                except Exception as e:
                    duration_ms = (time.monotonic() - start) * 1000
                    accumulator.record(
                        activity_name,
                        inputs=inputs,
                        outputs=None,
                        error=str(e),
                        started_at=started_at,
                        ended_at=datetime.now(timezone.utc).isoformat(),
                        duration_ms=duration_ms,
                    )
                    raise  # RE-RAISE — never swallow.

            return sync_wrapper  # type: ignore

    return decorator
