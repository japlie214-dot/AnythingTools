# tools/scraper/hitl.py
"""Human-in-the-Loop (HITL) escalation management for the scraper.

Refactored from TTY-bound input() to API-addressable HitlResolutionRegistry.
The worker thread blocks on threading.Event.wait(); POST /api/jobs/{id}/resume
with {decision: "proceed"|"skip"|"cancel"} unblocks it.

CRITICAL: Before blocking, we resolve the JobCompletionRegistry future so the
sync API's `await future` unblocks and returns {status: PAUSED_FOR_HITL} to the
LLM agent. Without this, the API hangs forever (per Pushback 1). The LLM then
calls POST /api/jobs/{id}/resume, which registers a NEW future; after the worker
unblocks and the tool finishes, the worker resolves the new future with the
terminal state.
"""
import threading
from enum import Enum
from datetime import datetime, timezone

from utils.hitl_resolution import hitl_registry
from bot.engine.completion_registry import job_completion_registry
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


class ValidationAction(Enum):
    """Routing decision for a validation failure."""
    PROCEED = "proceed"
    AUTO_SKIP = "auto_skip"
    HUMAN_HELP = "human_help"


class HITLState:
    def __init__(self):
        self.pending_url = None
        self.pending_reason = None
        self.lock = threading.Lock()
        # `decision` Event retained for backward compat with any code that
        # reads HITLState internals; the canonical blocking primitive is now
        # hitl_registry.wait(job_id).
        self.decision = threading.Event()
        self.decision_result = None

    def request_decision(self, job_id: str | None, url: str, reason: str) -> str:
        """Block the worker thread until the operator POSTs /resume.

        Returns "proceed" | "skip" | "cancel". The job's status is transitioned
        to PAUSED_FOR_HITL via enqueue_write; the SSE projector observes this
        via logs.status_state and emits the `paused` event.

        BEFORE blocking, resolves the JobCompletionRegistry future with
        {status: PAUSED_FOR_HITL, url, reason} so the sync API unblocks.
        """
        with self.lock:
            self.pending_url = url
            self.pending_reason = reason
            self.decision.clear()
            self.decision_result = None

        if job_id:
            from database.writer import enqueue_write, wait_for_writes
            # Mark the job paused in sumanal.db. The logger's status_state
            # side-effect at utils/logger/core.py:144-150 ALSO writes
            # PAUSED_FOR_HITL to logs.db. SSE phase derives from logs.db only.
            enqueue_write(
                "UPDATE jobs SET status = 'PAUSED_FOR_HITL', updated_at = ? WHERE job_id = ?",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
            # Wait for the write to commit so the SSE projector (which polls
            # logs.db, not sumanal.db) sees the status_state transition.
            # The dual_log call below ALSO enqueues a logs.db write with
            # status_state='PAUSED_FOR_HITL' — that's the one the SSE projector
            # keys off of.
            try:
                import asyncio
                loop = asyncio.get_running_loop()
                asyncio.run_coroutine_threadsafe(wait_for_writes(timeout=5.0), loop).result()
            except RuntimeError:
                import asyncio
                asyncio.run(wait_for_writes(timeout=5.0))

        # Log the pause request. The dual_log call's status_state='PAUSED_FOR_HITL'
        # writes to logs.db via logs_enqueue_write — this is the canonical
        # signal the SSE projector reads.
        log.dual_log(
            tag="Scraper:HITL:Request",
            message="Validation failed - awaiting human input",
            level="WARNING",
            status_state="PAUSED_FOR_HITL",
            payload={"url": url, "reason": reason, "job_id": job_id},
        )

        # CRITICAL: Resolve the completion registry BEFORE blocking.
        # The sync API's `await future` unblocks and returns PAUSED_FOR_HITL
        # to the LLM agent. The LLM then calls POST /api/jobs/{id}/resume,
        # which registers a NEW future for the terminal state.
        if job_id:
            job_completion_registry.resolve(job_id, {
                "job_id": job_id,
                "status": "PAUSED_FOR_HITL",
                "result": None,
                "error": None,
                "tool_name": "scraper",
                "hitl_url": url,
                "hitl_reason": reason,
            })

        # Block on the registry. POST /resume calls hitl_registry.set_decision().
        # No timeout: the worker stays paused indefinitely until the operator
        # decides. This matches the original input() behavior.
        decision = hitl_registry.wait(job_id) if job_id else "cancel"

        if job_id:
            from database.writer import enqueue_write
            new_status = "CANCELLING" if decision == "cancel" else "RUNNING"
            enqueue_write(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                (new_status, datetime.now(timezone.utc).isoformat(), job_id),
            )

        with self.lock:
            self.decision_result = decision
            self.pending_url = None
            self.pending_reason = None

        return decision


_hitl_state = HITLState()
