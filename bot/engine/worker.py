"""bot/engine/worker.py

Unified Worker Manager with crash recovery and caller-level locking.

Replaces the old worker/manager.py with centralized agent execution,
proper session continuity, and INTERRUPTED job recovery.
"""

import threading
import time
import json
import asyncio
from typing import Dict, Set
from datetime import datetime, timezone

import config
from utils.logger.core import get_dual_logger
from database.connection import DatabaseManager
from database.writer import enqueue_write
from utils.id_generator import ULID
from utils.context_helpers import spawn_thread_with_context
from tools.registry import REGISTRY
from bot.core.agent import UnifiedAgent

log = get_dual_logger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UnifiedWorkerManager:
    """Poll jobs table and execute via Unified Agent with caller locks."""
    
    def __init__(self, poll_interval: float = 1.0):
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_jobs: Dict[str, threading.Thread] = {}
        self._active_callers: Set[str] = set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="unified-worker-manager", daemon=True)
        self._thread.start()
        log.dual_log(tag="Worker:Manager:Start", message="Unified WorkerManager started.")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run_loop(self) -> None:
        """Poll for jobs and spawn execution threads."""
        while not self._stop_event.is_set():
            try:
                # Refresh registry for new tools
                REGISTRY.load_all()
                
                conn = DatabaseManager.get_read_connection()
                # Prioritize INTERRUPTED (recovery) jobs, then QUEUED
                rows = conn.execute(
                    "SELECT job_id, session_id, tool_name, args_json, status FROM jobs "
                    "WHERE status IN ('QUEUED', 'INTERRUPTED') ORDER BY status ASC, created_at ASC LIMIT 5"
                ).fetchall()
                
            except Exception as e:
                log.dual_log(tag="Worker:Manager:Poll", message=f"DB poll failed: {e}", level="WARNING")
                time.sleep(self.poll_interval)
                continue

            for r in rows:
                job_id = r["job_id"]
                session_id = str(r["session_id"])
                tool_name = r["tool_name"]
                status = r["status"]
                
                try:
                    args = json.loads(r["args_json"] or "{}")
                except Exception:
                    args = {}

                # Session-Level Lock: prevent concurrent execution for same session_id
                if session_id in self._active_callers:
                    continue

                # Register session as active
                self._active_callers.add(session_id)
                
                # Mark job as RUNNING
                ts = now_iso()
                enqueue_write(
                    "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                    ("RUNNING", ts, job_id),
                )

                # Recovery logic for INTERRUPTED jobs
                if status == "INTERRUPTED":
                    recovery_msg = (
                        "⚠️ RESUMPTION NOTICE: The system recovered from an interruption. "
                        "The browser has been restarted at Google. Verify your location before "
                        "proceeding. Consult job_items to review completed steps."
                    )
                    enqueue_write(
                        "INSERT INTO execution_ledger (job_id, session_id, role, content, char_count) VALUES (?, ?, ?, ?, ?)",
                        (job_id, session_id, "system", recovery_msg, len(recovery_msg))
                    )

                # Spawn execution thread
                t = spawn_thread_with_context(
                    self._run_job,
                    args=(job_id, session_id, tool_name, args),
                    name=f"job-{job_id}",
                    daemon=True
                )
                self._active_jobs[job_id] = t

            time.sleep(self.poll_interval)

    def _run_job(self, job_id: str, session_id: str, tool_name: str, args: dict) -> None:
        """Execute a single job using the Unified Agent."""
        try:
            # Map public tool to initial mode
            mode_map = {
                "research": "Analyst",
                "scraper": "Scout",
                "finance": "Quant",
                "draft_editor": "Editor",
                "publisher": "Herald"
            }
            initial_mode = mode_map.get(tool_name, "Analyst")

            async def telemetry_cb(update):
                """Placeholder telemetry callback."""
                pass

            agent = UnifiedAgent(job_id, session_id, initial_mode)
            result = asyncio.run(agent.run(telemetry_cb, tool_name=tool_name, **args))

            status_str = result.get("status", "FAILED")
            payload_json = json.dumps(result, ensure_ascii=False)

            enqueue_write(
                "UPDATE jobs SET status = ?, result_json = ?, updated_at = ? WHERE job_id = ?",
                (status_str, payload_json, now_iso(), job_id),
            )
            
        except Exception as e:
            log.dual_log(tag="Worker:Job:Crashed", message=f"Job {job_id} crashed: {e}", level="ERROR", exc_info=e)
            enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ("FAILED", now_iso(), job_id))
            
        finally:
            # Always clean up session lock
            if session_id in self._active_callers:
                self._active_callers.remove(session_id)
            if job_id in self._active_jobs:
                del self._active_jobs[job_id]


# Module-level singleton
_manager = UnifiedWorkerManager()


def get_manager() -> UnifiedWorkerManager:
    """Get the singleton unified worker manager."""
    return _manager
