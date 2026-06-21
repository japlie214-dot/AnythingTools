# api/sse/shutdown.py
"""Process-wide registry for SSE shutdown coordination.

app.py:125 calls os._exit(1) which kills SSE generators without yielding a
final event. This registry exposes an asyncio.Event that the lifespan
teardown sets BEFORE the 60s _active_jobs drain. The projector checks it each iteration.
"""
import asyncio
from typing import Optional

_shutdown_event: Optional[asyncio.Event] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def init_shutdown_registry(loop: asyncio.AbstractEventLoop) -> None:
    """Called from lifespan startup to bind the Event to the running loop.

    asyncio.Event MUST be created on the loop that will await it. Ref:
    https://docs.python.org/3/library/asyncio-sync.html#asyncio.Event
    """
    global _shutdown_event, _loop
    _loop = loop
    _shutdown_event = asyncio.Event()


def signal_shutdown() -> None:
    """Called from lifespan teardown. Schedules _shutdown_event.set() on the
    bound loop. Safe to call from any thread (uses call_soon_threadsafe).
    """
    if _shutdown_event is not None and _loop is not None:
        _loop.call_soon_threadsafe(_shutdown_event.set)


async def wait_for_shutdown(timeout: Optional[float] = None) -> bool:
    """Awaited by SSE projectors. Returns True if shutdown was signaled."""
    if _shutdown_event is None:
        return False
    try:
        await asyncio.wait_for(_shutdown_event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


def is_shutting_down() -> bool:
    """Non-async check for shutdown state."""
    return _shutdown_event is not None and _shutdown_event.is_set()
