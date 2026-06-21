"""bot/engine/worker.py

Unified Worker Manager with crash recovery and caller-level locking.

The worker polls the jobs table, spawns execution threads for each job,
and persists the terminal state directly to the database. The previous
AnythingLLM HTTP callback delivery has been removed — real-time progress
is now streamed via SSE (see utils/sse/broker.py and api/routes.py).
"""

import threading
import time
import json
import asyncio
from typing import Dict, Any
from datetime import datetime, timezone

import config
from utils.logger.core import get_dual_logger
from database.connection import DatabaseManager
from database.writer import enqueue_write
from utils.id_generator import ULID
from utils.context_helpers import spawn_thread_with_context
from tools.registry import REGISTRY

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
        """Poll for QUEUED and INTERRUPTED jobs and spawn execution threads."""
        while not self._stop_event.is_set():
            try:
                conn = DatabaseManager.get_read_connection()
                # Poll QUEUED and INTERRUPTED jobs only.
                # PENDING_CALLBACK has been removed from the enum (Step 4.2.4);
                # the callback retry loop is gone.
                rows = conn.execute(
                    "SELECT job_id, session_id, tool_name, args_json, status, result_json, retry_count FROM jobs "
                    "WHERE status IN ('QUEUED', 'INTERRUPTED') "
                    "ORDER BY status ASC, created_at ASC LIMIT 5"
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
                        "RESUMPTION NOTICE: The system recovered from an interruption. "
                        "The browser has been restarted at Google. Verify your location before "
                        "proceeding. Consult job_items to review completed steps."
                    )
                    log.dual_log(tag="Worker:Job:Recovery", message="Recovered interrupted job", payload={"job_id": job_id, "recovery_notice": recovery_msg})

                # Prepare cancellation flag
                flag = threading.Event()
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
        """Execute a single job and commit terminal state directly.

        The previous implementation deferred terminal state commitment
        until the AnythingLLM HTTP callback succeeded. That is gone.
        Terminal state is now set immediately on tool return:
          - Tool returns successfully → COMPLETED
          - Tool raises / returns failure → FAILED
          - Tool pauses for HITL → PAUSED_FOR_HITL
          - Crash → INTERRUPTED (retry) or ABANDONED (3 crashes)
        """
        try:
            async def telemetry_cb(update):
                """Telemetry callback — intercepted by the SSE broker via
                the logs.db pipeline. No-op here; the dual_log in
                BaseTool.execute handles persistence."""
                pass

            tool_instance = REGISTRY.create_tool_instance(tool_name)
            if not tool_instance:
                result = {"status": "FAILED", "result": f"Tool {tool_name} not found"}
            else:
                from bot.engine.tool_runner import run_tool_safely
                res = asyncio.run(run_tool_safely(tool_instance, args, telemetry_cb, job_id=job_id, session_id=session_id, cancellation_flag=cancellation_flag))
                if res.success:
                    # Tools now return plain markdown strings. Try to parse
                    # as JSON for backward compat with tools that haven't
                    # been refactored yet; if it fails, use the raw string.
                    try:
                        parsed = json.loads(res.output)
                        result = {"status": "COMPLETED", "result": parsed}
                    except Exception:
                        result = {"status": "COMPLETED", "result": res.output}
                else:
                    result = {"status": "FAILED", "result": res.output}

            # Normalize result to a plain dict.
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
                    normal = dict(result)
                except Exception:
                    normal = {"status": "FAILED", "result": str(result)}

            attachments = res.attachment_paths or [] if 'res' in dir() else []
            if attachments:
                normal["attachment_paths"] = attachments

            status_str = normal.get("status", "FAILED")
            payload_json = json.dumps(normal, ensure_ascii=False)

            # Commit terminal state directly — no callback delivery step.
            # The SSE broker picks up the status change via the logs.db
            # pipeline and the jobs table update.
            enqueue_write(
                "UPDATE jobs SET status = ?, result_json = ?, updated_at = ? WHERE job_id = ?",
                (status_str, payload_json, now_iso(), job_id),
            )

            # Log the terminal state transition for SSE consumers.
            from database.logs_writer import logs_enqueue_write
            logs_enqueue_write(
                "INSERT INTO logs (id, job_id, tag, level, status_state, message, payload_json, event_id, error_json, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ULID.generate(), job_id, "Worker:Job:Terminal", "INFO", status_str,
                 f"Job reached terminal state: {status_str}",
                 json.dumps({"status": status_str, "tool": tool_name, "output_len": len(normal.get("result", ""))}),
                 ULID.generate(), None, now_iso())
            )

            # Reset errors on success
            if job_id in self._system_errors:
                del self._system_errors[job_id]

        except Exception as e:
            err_str = str(e)
            if err_str.startswith("PAUSED_FOR_HITL:"):
                msg = err_str.split(":", 1)[1].strip() if ":" in err_str else err_str
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
            # Export job logs to file for any terminal failure state.
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
                    except Exception:
                        pass
            except Exception:
                pass

            if job_id in self._active_jobs:
                del self._active_jobs[job_id]


# Module-level singleton
_manager = UnifiedWorkerManager()


def get_manager() -> UnifiedWorkerManager:
    """Get the singleton unified worker manager."""
    return _manager
