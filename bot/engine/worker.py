"""bot/engine/worker.py

Unified Worker Manager with crash recovery and caller-level locking.

Replaces the old worker/manager.py with centralized agent execution,
proper session continuity, and INTERRUPTED job recovery.
"""

import threading
import time
import json
import asyncio
from typing import Dict, Set, Any
from datetime import datetime, timezone

import config
from utils.logger.core import get_dual_logger
from database.connection import DatabaseManager
from database.writer import enqueue_write
from utils.id_generator import ULID
from utils.context_helpers import spawn_thread_with_context
from tools.registry import REGISTRY
from utils.hitl_resolution import hitl_registry

log = get_dual_logger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()




class UnifiedWorkerManager:
    """Poll jobs table and execute tools directly."""
    
    def __init__(self, poll_interval: float = 1.0):
        import collections
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_jobs: Dict[str, threading.Thread] = {}
        self._system_errors = collections.defaultdict(int)
        self.cancellation_flags: Dict[str, threading.Event] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="unified-worker-manager", daemon=True)
        self._thread.start()
        log.dual_log(tag="Worker:Manager:Start", message="Unified WorkerManager started.", payload={"status": "STARTED", "poll_interval": self.poll_interval})

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run_loop(self) -> None:
        """Poll for jobs and spawn execution threads (no session locks)."""
        while not self._stop_event.is_set():
            try:
                # Tool registry is loaded at startup; skip repeated loads here.
                pass

                
                conn = DatabaseManager.get_read_connection()
                # Prioritize INTERRUPTED (recovery) jobs, then QUEUED
                rows = conn.execute(
                    "SELECT job_id, session_id, tool_name, args_json, status, result_json, retry_count FROM jobs "
                    "WHERE status IN ('QUEUED', 'INTERRUPTED') "
                    "ORDER BY status ASC, created_at ASC LIMIT 5",
                ).fetchall()
                
            except Exception as e:
                log.dual_log(tag="Worker:Manager:Poll", message="DB poll failed", level="WARNING", payload={"error": str(e)})
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
                    log.dual_log(tag="Worker:Job:Recovery", message="Recovered interrupted job", payload={"job_id": job_id, "recovery_notice": recovery_msg})

                # Prepare cancellation flag. Per Pushback 5: do NOT clobber an
                # existing flag — DELETE /jobs/{id} may have set it while the
                # job was still QUEUED, and overwriting it would drop the
                # cancellation request.
                with threading.Lock():
                    existing = self.cancellation_flags.get(job_id)
                    if existing is not None and existing.is_set():
                        # Operator already cancelled; honor it by not starting
                        # the job. The CANCELLING status was set by DELETE.
                        log.dual_log(tag="Worker:Job:SkipCancelled", message=f"Skipping cancelled job {job_id}", payload={"job_id": job_id})
                        continue
                    flag = existing or threading.Event()
                    self.cancellation_flags[job_id] = flag

                # Spawn execution thread
                t = spawn_thread_with_context(
                    self._run_job,
                    args=(job_id, session_id, tool_name, args, flag),
                    name=f"job-{job_id}",
                    daemon=True
                )
                self._active_jobs[job_id] = t

            time.sleep(self.poll_interval)


    def _run_job(self, job_id: str, session_id: str, tool_name: str, args: dict, cancellation_flag: threading.Event) -> None:
        """Execute a single job using direct tool invocation."""
        try:
            async def telemetry_cb(update):
                """Placeholder telemetry callback."""
                pass

            attachments = []
            tool_instance = REGISTRY.create_tool_instance(tool_name)
            if not tool_instance:
                result = {"status": "FAILED", "result": f"Tool {tool_name} not found"}
            else:
                from bot.engine.tool_runner import run_tool_safely
                res = asyncio.run(run_tool_safely(tool_instance, args, telemetry_cb, job_id=job_id, session_id=session_id, cancellation_flag=cancellation_flag))
                attachments = res.attachment_paths or []
                if res.success:
                    # Attempt to parse JSON output for structured payloads
                    try:
                        parsed = json.loads(res.output)
                        result = {"status": "COMPLETED", "result": parsed}
                    except Exception:
                        result = {"status": "COMPLETED", "result": res.output}
                else:
                    result = {"status": "FAILED", "result": res.output}

            # Normalize result to a plain dict. Defensive handling is important
            # because some code paths (or regressions) may accidentally return a
            # sqlite3.Row or other mapping-like object which does not implement
            # the full dict interface expected below (e.g. .get()). Coerce
            # safely to avoid crashing the worker thread.
            try:
                import sqlite3 as _sqlite3
            except Exception:
                _sqlite3 = None

            if isinstance(result, dict):
                normal = result
            elif _sqlite3 is not None and isinstance(result, _sqlite3.Row):
                normal = dict(result)
            else:
                try:
                    # Try to coerce any mapping-like or object to dict
                    normal = dict(result)
                except Exception:
                    # Fallback to a minimal serializable dict
                    normal = {"status": "FAILED", "result": str(result)}

            if attachments:
                normal["attachment_paths"] = attachments

            status_str = normal.get("status", "FAILED")
            payload_json = json.dumps(normal, ensure_ascii=False)

            # Always write the terminal status directly. The old callback
            # retry path is removed — SSE clients read result_json via the
            # completed event.
            enqueue_write(
                "UPDATE jobs SET status = ?, result_json = ?, updated_at = ? WHERE job_id = ?",
                (status_str, payload_json, now_iso(), job_id),
            )
            
            # Reset errors on success
            if job_id in self._system_errors:
                del self._system_errors[job_id]
                
        except Exception as e:
            err_str = str(e)
            # HitlPaused carries a .reason attr; check type instead of string
            # prefix matching (the old approach was brittle — see Pushback).
            from tools.base import HitlPaused
            if isinstance(e, HitlPaused):
                msg = e.reason if hasattr(e, 'reason') else str(e)
                log.dual_log(tag="Worker:Job:Paused", message="Job paused for HITL", level="WARNING", payload={"job_id": job_id, "reason": msg})
                enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ("PAUSED_FOR_HITL", now_iso(), job_id))
            else:
                self._system_errors[job_id] += 1
                if self._system_errors[job_id] >= 3:
                    log.dual_log(tag="Worker:Job:Abandoned", message=f"Job {job_id} ABANDONED after 3 consecutive system errors", level="CRITICAL", payload={"job_id": job_id, "error": str(e)})
                    enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ("ABANDONED", now_iso(), job_id))
                    del self._system_errors[job_id]
                else:
                    log.dual_log(tag="Worker:Job:Crashed", message=f"Job {job_id} crashed", level="ERROR", exc_info=e, payload={"job_id": job_id, "attempt": self._system_errors[job_id], "error": str(e)})
                    time.sleep(10)
                    enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ("INTERRUPTED", now_iso(), job_id))
            
        finally:
            # Clean up HITL registry state so /resume doesn't deliver to a dead worker.
            try:
                hitl_registry.clear(job_id)
            except Exception:
                pass
            # Export job logs to file for any terminal failure state. We must wait
            # briefly for the asynchronous logs writer queue to drain so that the
            # final fatal log entries are persisted before we snapshot them.
            try:
                final_status = None
                try:
                    conn = DatabaseManager.get_read_connection()
                    row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                    if row:
                        final_status = row["status"]
                except Exception:
                    final_status = None

                if final_status in ("FAILED", "ABANDONED", "PARTIAL"):
                    try:
                        from database.logs_writer import logs_write_queue
                        import time as _t
                        start_wait = _t.time()
                        while not logs_write_queue.empty() and (_t.time() - start_wait) < 60:
                            _t.sleep(0.5)

                        from utils.error_export import export_job_logs_to_file
                        export_job_logs_to_file(job_id, final_status)
                    except Exception as export_err:
                        log.dual_log(
                            tag="Worker:LogExport:Write",
                            message=f"Failed to export job logs: {export_err}",
                            level="WARNING",
                            payload={"job_id": job_id, "error": str(export_err)}
                        )
            except Exception:
                pass

            if job_id in self._active_jobs:
                del self._active_jobs[job_id]


# Module-level singleton
_manager = UnifiedWorkerManager()


def get_manager() -> UnifiedWorkerManager:
    """Get the singleton unified worker manager."""
    return _manager
