# bot/engine/completion_registry.py
"""Process-wide registry mapping job_id -> asyncio.Future.

The worker thread resolves futures via loop.call_soon_threadsafe; API
endpoints await futures with a timeout and a client-disconnect race.

Thread-safety: the _futures dict is guarded by a threading.Lock (NOT
asyncio.Lock) because the worker thread is not on the event loop.
Ref: https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.call_soon_threadsafe
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Optional

from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)


class JobCompletionRegistry:
    """Singleton registry. One future per job_id per "await cycle".

    An await cycle is: API registers a future -> worker resolves it -> API
    returns. For HITL, there are two cycles:
      1. API awaits -> worker resolves with {status: PAUSED_FOR_HITL} -> API returns.
      2. LLM calls /resume -> API registers a NEW future -> worker (after
         unblocking) resolves with terminal state -> API returns.
    """

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind to the FastAPI event loop. Called from lifespan startup.
        MUST be called from within the running loop (asyncio.get_running_loop())."""
        self._loop = loop
        log.dual_log(
            tag="CompletionRegistry:BindLoop",
            message="JobCompletionRegistry bound to event loop",
            payload={"loop_id": id(loop)},
        )

    def register(self, job_id: str) -> asyncio.Future:
        """Create a new future for this job_id. Called by the API endpoint.
        MUST be called from the event loop thread (asyncio.Future is loop-bound).
        Ref: https://docs.python.org/3/library/asyncio-future.html"""
        if self._loop is None:
            raise RuntimeError("JobCompletionRegistry.bind_loop() not called")
        future = self._loop.create_future()
        with self._lock:
            # If a stale future exists (e.g., previous await cycle timed out
            # but the worker hasn't resolved yet), cancel it to avoid leaks.
            old = self._futures.get(job_id)
            if old is not None and not old.done():
                old.cancel()
            self._futures[job_id] = future
        return future

    def resolve(self, job_id: str, terminal_state: dict[str, Any]) -> None:
        """Resolve the future for job_id with terminal_state.
        Thread-safe: schedules future.set_result on the bound loop via
        call_soon_threadsafe. Called from the WORKER thread.
        Ref: https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.call_soon_threadsafe"""
        if self._loop is None:
            log.dual_log(
                tag="CompletionRegistry:NoLoop",
                message="resolve() called but loop not bound; dropping",
                level="WARNING",
                payload={"job_id": job_id, "terminal_state": terminal_state},
            )
            return
        with self._lock:
            future = self._futures.get(job_id)
        if future is None:
            # No API request awaiting (e.g., job ran via legacy 202 path).
            # Not an error — just nothing to resolve.
            return
        if future.done():
            # Already resolved (e.g., duplicate call). Log and return.
            log.dual_log(
                tag="CompletionRegistry:AlreadyDone",
                message="Future already resolved; ignoring duplicate",
                level="DEBUG",
                payload={"job_id": job_id},
            )
            return
        # Thread-safe resolution: schedule set_result on the loop thread.
        # asyncio.Future.set_result is NOT thread-safe; must go through the loop.
        try:
            self._loop.call_soon_threadsafe(self._safe_set_result, future, terminal_state)
        except RuntimeError:
            # Loop closed (app shutting down). Log and drop.
            log.dual_log(
                tag="CompletionRegistry:LoopClosed",
                message="Event loop closed; cannot resolve future",
                level="WARNING",
                payload={"job_id": job_id},
            )

    def _safe_set_result(self, future: asyncio.Future, value: dict[str, Any]) -> None:
        """Called on the loop thread via call_soon_threadsafe."""
        if not future.done():
            future.set_result(value)

    def cleanup(self, job_id: str) -> None:
        """Remove the future for job_id. Called by the API endpoint after
        it finishes awaiting (success, timeout, or disconnect)."""
        with self._lock:
            self._futures.pop(job_id, None)


# Process-wide singleton. WEB_CONCURRENCY=1 is enforced at app.py:36-37,
# so this singleton is shared by the worker thread and all FastAPI handlers.
job_completion_registry = JobCompletionRegistry()
