# api/sse/log_notify.py
"""Per-job_id asyncio.Event bus poked by the logs writer thread.

Wakes SSE projectors immediately when new log rows are committed for their
job_id, eliminating the 1s polling latency. Falls back to 1s polling if the
Event is never set (e.g., logs writer crashed).

Thread-safety: the events dict is guarded by _lock. Event.set() is scheduled
on the bound loop via call_soon_threadsafe because the logs writer runs in
its own thread and asyncio.Event is not thread-safe to set directly. Ref:
https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.call_soon_threadsafe
"""
import asyncio
import threading
from typing import Optional

_lock = threading.Lock()
_events: dict[str, asyncio.Event] = {}
_loop: Optional[asyncio.AbstractEventLoop] = None


def init_log_notify_bus(loop: asyncio.AbstractEventLoop) -> None:
    """Bind the bus to the FastAPI event loop. Called from lifespan startup."""
    global _loop
    _loop = loop


def register(job_id: str) -> asyncio.Event:
    """Create (or reuse) the asyncio.Event for a job_id. Called by the SSE
    projector when it starts streaming. MUST be called from the FastAPI loop.
    """
    if _loop is None:
        # Defensive: if init wasn't called, return a never-set Event so the
        # projector falls back to polling.
        return asyncio.Event()
    with _lock:
        if job_id not in _events:
            _events[job_id] = asyncio.Event()
        return _events[job_id]


def notify(job_ids: set[str]) -> None:
    """Called by the logs writer thread after each commit. Schedules
    Event.set() on the FastAPI loop via call_soon_threadsafe.
    """
    if _loop is None:
        return
    for jid in job_ids:
        with _lock:
            ev = _events.get(jid)
        if ev is not None:
            try:
                _loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                # Loop closed (app shutting down) — ignore.
                pass


def clear(job_id: str) -> None:
    """Called by the SSE projector when the stream ends."""
    with _lock:
        _events.pop(job_id, None)
