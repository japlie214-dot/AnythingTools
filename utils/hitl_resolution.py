# utils/hitl_resolution.py
"""Process-wide registry for resolving HITL pauses via the API.

Replaces the TTY-bound input() call in tools/scraper/hitl.py. The worker
thread blocks on a threading.Event; POST /api/jobs/{id}/resume unblocks it
with a decision string ("proceed" | "skip" | "cancel").

Thread-safety: all mutations are guarded by _lock. The Event.wait() call
releases the GIL, allowing the FastAPI event loop to process /resume.
"""
import threading
from typing import Optional

# Valid decisions the operator may submit via POST /resume.
# Mirrors the original input() options in tools/scraper/hitl.py:54.
VALID_DECISIONS = frozenset({"proceed", "skip", "cancel"})


class _HitlResolutionRegistry:
    def __init__(self) -> None:
        self._events: dict[str, threading.Event] = {}
        self._decisions: dict[str, str] = {}
        self._lock = threading.Lock()

    def register(self, job_id: str) -> threading.Event:
        """Create (or reuse) the Event for a job_id. Called by the worker
        thread BEFORE entering HitlResolutionRegistry.wait().

        Idempotent: if called twice for the same job_id without an intervening
        clear(), the existing Event is returned. This handles the case where
        a tool re-enters HITL multiple times in one job run.
        """
        with self._lock:
            if job_id not in self._events:
                self._events[job_id] = threading.Event()
                self._decisions[job_id] = ""
            return self._events[job_id]

    def wait(self, job_id: str, timeout: Optional[float] = None) -> str:
        """Block the calling (worker) thread until /resume sets a decision.

        Returns the decision string, or "cancel" on timeout/EOF-like conditions
        to preserve the original input() EOFError fallback behavior at
        tools/scraper/hitl.py:58-59.
        """
        event = self.register(job_id)
        fired = event.wait(timeout=timeout)
        if not fired:
            return "cancel"
        with self._lock:
            return self._decisions.get(job_id, "proceed")

    def set_decision(self, job_id: str, decision: str) -> bool:
        """Called by POST /api/jobs/{id}/resume. Returns True if the decision
        was delivered to a waiting worker, False if no worker is registered
        (e.g., the job is not actually paused, or the worker already timed out).
        """
        if decision not in VALID_DECISIONS:
            return False
        with self._lock:
            if job_id not in self._events:
                return False
            self._decisions[job_id] = decision
            self._events[job_id].set()
            return True

    def is_registered(self, job_id: str) -> bool:
        """Check whether a worker is currently blocked waiting for a decision.
        Used by /resume to distinguish HITL-paused jobs (worker blocked) from
        INTERRUPTED jobs (worker not blocked, needs resume handler path).
        """
        with self._lock:
            return job_id in self._events and self._events[job_id].is_set() is False

    def clear(self, job_id: str) -> None:
        """Remove all state for a job_id. Called in worker._run_job finally."""
        with self._lock:
            self._events.pop(job_id, None)
            self._decisions.pop(job_id, None)


# Process-wide singleton. WEB_CONCURRENCY=1 is enforced at app.py:46-47,
# so this singleton is shared by the worker thread and all FastAPI handlers.
hitl_registry = _HitlResolutionRegistry()
