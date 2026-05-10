# tools/scraper/hitl.py
"""Human-in-the-Loop (HITL) escalation management for the scraper."""

import threading
from enum import Enum

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
        self.decision = threading.Event()
        self.decision_result = None

    def request_decision(self, job_id: str | None, url: str, reason: str) -> str:
        with self.lock:
            self.pending_url = url
            self.pending_reason = reason
            self.decision.clear()
            self.decision_result = None

        if job_id:
            from database.writer import enqueue_write, wait_for_writes
            from datetime import datetime, timezone
            import asyncio
            enqueue_write(
                "UPDATE jobs SET status = 'PAUSED_FOR_HITL', updated_at = ? WHERE job_id = ?",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
            try:
                loop = asyncio.get_running_loop()
                asyncio.run_coroutine_threadsafe(wait_for_writes(timeout=5.0), loop).result()
            except RuntimeError:
                asyncio.run(wait_for_writes(timeout=5.0))

        from utils.logger import get_dual_logger
        log = get_dual_logger(__name__)
        log.dual_log(
            tag="Scraper:HITL:Request",
            message=f"Validation failed - awaiting human input",
            level="WARNING",
            payload={"url": url, "reason": reason, "job_id": job_id},
        )

        print(f"\n\n[!!!] HITL VALIDATION ALERT")
        print(f">>> URL: {url}")
        print(f">>> Reason: {reason}")
        print(">>> Type 'ENTER' to force PROCEED, 'SKIP' to skip URL, or 'CANCEL' to abort job.")

        try:
            user_input = input("Decision: ").strip().upper()
        except EOFError:
            user_input = "CANCEL"

        if job_id:
            from database.writer import enqueue_write
            from datetime import datetime, timezone
            if user_input == "CANCEL":
                enqueue_write(
                    "UPDATE jobs SET status = 'CANCELLING', updated_at = ? WHERE job_id = ?",
                    (datetime.now(timezone.utc).isoformat(), job_id),
                )
            else:
                enqueue_write(
                    "UPDATE jobs SET status = 'RUNNING', updated_at = ? WHERE job_id = ?",
                    (datetime.now(timezone.utc).isoformat(), job_id),
                )

        with self.lock:
            if user_input == "SKIP":
                self.decision_result = "skip"
            elif user_input == "CANCEL":
                self.decision_result = "cancel"
            else:
                self.decision_result = "proceed"
            self.pending_url = None
            self.pending_reason = None

        return self.decision_result

_hitl_state = HITLState()
