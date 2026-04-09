# worker/manager.py
"""
Background WorkerManager that polls the `jobs` table and executes queued jobs.

Responsibilities:
- Poll `jobs` table for QUEUED work, claim jobs, and run them in background
  threads (one thread per job).
- Maintain per-job cancellation flags (threading.Event) exposed via
  WorkerManager.cancellation_flags so API cancel endpoints can set them.
- Periodically enqueue heartbeat updates (jobs.updated_at) so stale jobs
  can be detected by operators.

Design notes:
- All persistent updates use `enqueue_write()` so writes go through the WAL-safe
  single-writer queue.
- The Manager is deliberately simple: a single manager thread claims jobs and
  spawns worker threads. This keeps the DB-side claiming logic linear and
  resilient across restarts (jobs remain in DB until processed).
"""

import threading
import time
import json
import asyncio
from typing import Dict, Any
from datetime import datetime, timezone, timedelta

import config
from utils.logger.core import get_dual_logger
from database.connection import DatabaseManager
from database.writer import enqueue_write
from utils.id_generator import ULID
from utils.artifacts import artifact_relpath_for_http
from utils.context_helpers import spawn_thread_with_context, to_thread_with_context

# Import registry lazily to avoid import-time cycles; will call load_all() inside loop
from tools.registry import REGISTRY

log = get_dual_logger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerManager:
    def __init__(self, poll_interval: float = 1.0, heartbeat_interval: float = 60.0) -> None:
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_jobs: Dict[str, threading.Thread] = {}
        # Cancellation flags registered per-job_id
        self.cancellation_flags: Dict[str, threading.Event] = {}

        # Watchdog configuration (seconds)
        self.watch_interval = getattr(config, "JOB_WATCH_INTERVAL_SECONDS", 300)
        self.stale_threshold_seconds = getattr(config, "JOB_STALE_THRESHOLD_SECONDS", 8 * 3600)
        self._watch_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="anythingtools-worker-manager", daemon=True)
        self._thread.start()
        # Start watchdog thread
        self._watch_thread = threading.Thread(target=self._watchdog_loop, name="anythingtools-worker-watchdog", daemon=True)
        self._watch_thread.start()
        log.dual_log(tag="Worker:Manager:Start", message="WorkerManager started.")

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=timeout)
        log.dual_log(tag="Worker:Manager:Stop", message="WorkerManager stopped.")

    def _run_loop(self) -> None:
        last_heartbeat = time.time()
        while not self._stop_event.is_set():
            try:
                # Ensure registry is fresh so we can instantiate tool classes
                try:
                    REGISTRY.load_all()
                except Exception:
                    pass

                conn = DatabaseManager.get_read_connection()
                rows = conn.execute(
                    "SELECT job_id, tool_name, args_json FROM jobs WHERE status = 'QUEUED' ORDER BY created_at LIMIT 2"
                ).fetchall()
            except Exception as e:
                log.dual_log(tag="Worker:Manager:Poll", message=f"DB poll failed: {e}", level="WARNING", exc_info=e)
                time.sleep(self.poll_interval)
                continue

            for r in rows:
                job_id = r["job_id"]
                tool_name = r["tool_name"]
                args_json = r["args_json"] or "{}"
                try:
                    args = json.loads(args_json)
                except Exception:
                    args = {}

                # Claim job (best-effort): mark RUNNING via enqueue_write so readers
                # see the transition in WAL order.
                ts = now_iso()
                enqueue_write(
                    "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ? AND status = 'QUEUED'",
                    ("RUNNING", ts, job_id),
                )

                # Spawn a thread to run the job so the manager's poll loop stays lean.
                t = spawn_thread_with_context(self._run_job, args=(job_id, tool_name, args), name=f"job-{job_id}", daemon=True)
                self._active_jobs[job_id] = t

            # Periodic heartbeat for active jobs
            if time.time() - last_heartbeat >= self.heartbeat_interval:
                for jid in list(self._active_jobs.keys()):
                    try:
                        enqueue_write("UPDATE jobs SET updated_at = ? WHERE job_id = ?", (now_iso(), jid))
                    except Exception:
                        pass
                last_heartbeat = time.time()

            time.sleep(self.poll_interval)

    def _run_job(self, job_id: str, tool_name: str, args: dict) -> None:
        # Register cancellation flag for this job
        cancellation = threading.Event()
        self.cancellation_flags[job_id] = cancellation
        start_ts = time.time()
        try:
            tool = REGISTRY.create_tool_instance(tool_name)
            if tool is None:
                raise RuntimeError("Tool instantiation failed")

            async def telemetry_cb(update):
                # Simple telemetry bridge: write a lightweight job_logs row for visibility
                try:
                    ts = getattr(update, "timestamp", now_iso())
                    msg = getattr(update, "message", str(update))
                    state = getattr(update, "status", None)
                    # Enqueue a short job_log for immediate visibility
                    enqueue_write(
                        "INSERT INTO job_logs (id, job_id, tag, level, status_state, message, payload_json, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (ULID.generate(), job_id, "telemetry", "INFO", state, msg, None, ts),
                    )
                except Exception:
                    try:
                        log.dual_log(tag="Worker:Job:Telemetry", message="Failed to persist telemetry entry", level="WARNING")
                    except Exception:
                        pass

            # Execute the tool inside an isolated event loop
            result = asyncio.run(tool.execute(args, telemetry_cb, job_id=job_id, cancellation_flag=cancellation))

            # If the cancellation flag was set at any point during execution, mark CANCELLED.
            if cancellation.is_set():
                success = False
                status_str = "CANCELLED"
            else:
                success = getattr(result, "success", False)
                status_str = "COMPLETED" if success else "FAILED"

            # Update job status first
            enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", (status_str, now_iso(), job_id))

            # Construct standardized final payload envelope
            try:
                # Normalize result: attempt to parse JSON output otherwise wrap as text
                parsed_result = None
                raw_output = getattr(result, "output", None)
                if raw_output:
                    try:
                        parsed_result = json.loads(raw_output)
                    except Exception:
                        parsed_result = {"text": raw_output}

                artifacts_list = []
                for p in (getattr(result, "attachment_paths", []) or []):
                    try:
                        rel = artifact_relpath_for_http(p)
                    except Exception:
                        rel = str(p)
                    artifacts_list.append({"id": ULID.generate(), "relpath": rel, "metadata": {}})

                error_details = None
                if not success:
                    error_details = getattr(result, "diagnosis", None) or {"message": raw_output}

                payload = {
                    "status": status_str,
                    "result": parsed_result,
                    "artifacts": artifacts_list or None,
                    "error_details": error_details,
                    "metrics": {"duration_seconds": round(time.time() - start_ts, 3)},
                    "completed_at": now_iso(),
                }

                payload_json = json.dumps(payload, ensure_ascii=False, default=str)

                # Persist final payload into job_logs and jobs.result_json
                enqueue_write(
                    "INSERT INTO job_logs (id, job_id, tag, level, status_state, message, payload_json, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ULID.generate(), job_id, "Worker", "INFO", status_str, "Job finished", payload_json, now_iso()),
                )

                enqueue_write(
                    "UPDATE jobs SET result_json = ?, updated_at = ? WHERE job_id = ?",
                    (payload_json, now_iso(), job_id),
                )

            except Exception as e:
                # Best-effort: log and continue
                log.dual_log(tag="Worker:Job:Persist", message=f"Failed to persist final payload for {job_id}: {e}", level="WARNING", exc_info=e)

        except Exception as e:
            log.dual_log(tag="Worker:Job:Crashed", message=f"Job {job_id} crashed: {e}", level="ERROR", exc_info=e)
            enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ("FAILED", now_iso(), job_id))
        finally:
            # Clean up runtime maps
            try:
                del self.cancellation_flags[job_id]
            except Exception:
                pass
            try:
                del self._active_jobs[job_id]
            except Exception:
                pass
            # Background cleanup of images (performance-neutral)
            if getattr(config, "ENV", "prod") != "test":
                def _cleanup_job_images(jid: str):
                    import shutil, os
                    target_dir = f"data/temp/{jid}"
                    if os.path.exists(target_dir):
                        try:
                            shutil.rmtree(target_dir)
                        except Exception:
                            pass
                threading.Thread(target=_cleanup_job_images, args=(job_id,), daemon=True).start()

    def _watchdog_loop(self) -> None:
        """Periodically scan for RUNNING jobs with stale updated_at and mark them FAILED."""
        while not self._stop_event.is_set():
            try:
                # Mark jobs that haven't updated in > stale_threshold_seconds
                threshold_clause = f"datetime('now', '-{int(self.stale_threshold_seconds)} seconds')"
                conn = DatabaseManager.get_read_connection()
                rows = conn.execute(
                    f"SELECT job_id FROM jobs WHERE status = 'RUNNING' AND updated_at < {threshold_clause}"
                ).fetchall()
                for r in rows:
                    jid = r[0]
                    try:
                        enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ("FAILED", now_iso(), jid))
                        enqueue_write(
                            "INSERT INTO job_logs (id, job_id, tag, level, status_state, message, payload_json, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (ULID.generate(), jid, "Watchdog", "WARNING", "FAILED", "Marked as failed by watchdog due to inactivity", None, now_iso()),
                        )
                        # Notify user immediately
                        try:
                            from api.telegram_notifier import notify_user_sync
                            notify_user_sync(f"🚨 [WATCHDOG] Job {jid} marked as FAILED due to inactivity (> {self.stale_threshold_seconds}s).")
                        except Exception:
                            pass
                    except Exception:
                        pass
            except Exception:
                pass
            # Sleep until next pass
            time.sleep(self.watch_interval)


# Module-level singleton and accessor
_manager = WorkerManager()


def get_manager() -> WorkerManager:
    return _manager
