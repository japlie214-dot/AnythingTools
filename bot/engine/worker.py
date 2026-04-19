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
import httpx
import base64
import os
import mimetypes

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
        log.dual_log(tag="Worker:Manager:Start", message="Unified WorkerManager started.")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run_loop(self) -> None:
        """Poll for jobs and spawn execution threads (no session locks)."""
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
                    log.dual_log(tag="Worker:Job:Recovery", message=recovery_msg)

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

            status_str = normal.get("status", "FAILED")
            payload_json = json.dumps(normal, ensure_ascii=False)

            enqueue_write(
                "UPDATE jobs SET status = ?, result_json = ?, updated_at = ? WHERE job_id = ?",
                (status_str, payload_json, now_iso(), job_id),
            )

            # Invoke AnythingLLM callback for completed and partial jobs (non-blocking best-effort)
            if status_str in ("COMPLETED", "PARTIAL"):
                try:
                    self._invoke_anythingllm_callback(job_id, normal.get("result"), attachments)
                except Exception:
                    # Callback failures must not break worker execution
                    pass
            
            # Reset errors on success
            if job_id in self._system_errors:
                del self._system_errors[job_id]
                
        except Exception as e:
            err_str = str(e)
            if err_str.startswith("PAUSED_FOR_HITL:"):
                msg = err_str.split(":", 1)[1].strip() if ":" in err_str else err_str
                log.dual_log(tag="Worker:Job:Paused", message=f"Job {job_id} paused for HITL: {msg}", level="WARNING")
                enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ("PAUSED_FOR_HITL", now_iso(), job_id))
            else:
                self._system_errors[job_id] += 1
                if self._system_errors[job_id] >= 3:
                    log.dual_log(tag="Worker:Job:Abandoned", message=f"Job {job_id} ABANDONED after 3 consecutive system errors: {e}", level="CRITICAL", notify_user=True)
                    enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ("ABANDONED", now_iso(), job_id))
                    del self._system_errors[job_id]
                else:
                    log.dual_log(tag="Worker:Job:Crashed", message=f"Job {job_id} crashed (Attempt {self._system_errors[job_id]}/3). Sleeping 10s: {e}", level="ERROR", exc_info=e)
                    time.sleep(10)
                    enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ("INTERRUPTED", now_iso(), job_id))
            
        finally:
            if job_id in self._active_jobs:
                del self._active_jobs[job_id]

    def _invoke_anythingllm_callback(self, job_id: str, tool_output: Any, attachment_paths: list[str]) -> None:
        """Send the tool output and attachments back to AnythingLLM via HTTP POST.

        Files are Base64-encoded and included as data URIs to avoid multipart complexity.
        """
        if not getattr(config, "ANYTHINGLLM_BASE_URL", None) or not getattr(config, "ANYTHINGLLM_API_KEY", None):
            return

        url = f"{config.ANYTHINGLLM_BASE_URL.rstrip('/')}/api/v1/workspace/{config.ANYTHINGLLM_WORKSPACE_SLUG}/chat"
        headers = {"Authorization": f"Bearer {config.ANYTHINGLLM_API_KEY}", "Content-Type": "application/json"}

        attachments_payload = []
        for path in (attachment_paths or []):
            try:
                if not os.path.exists(path):
                    continue
                ext = os.path.splitext(path)[1].lower()
                mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
                with open(path, "rb") as f:
                    b64_data = base64.b64encode(f.read()).decode("utf-8")
                attachments_payload.append({
                    "name": os.path.basename(path),
                    "mime": mime,
                    "contentString": f"data:{mime};base64,{b64_data}",
                })
            except Exception as e:
                try:
                    log.dual_log(tag="Worker:Callback:File", message=f"Failed to encode {path}: {e}", level="WARNING")
                except Exception:
                    pass

        # Construct the payload. TOOL_RESULT_CORRELATION_ID ensures the caller can match callbacks.
        try:
            payload_body = json.dumps(tool_output, ensure_ascii=False) if not isinstance(tool_output, str) else tool_output
        except Exception:
            payload_body = str(tool_output)

        callback_payload = {
            "message": f"TOOL_RESULT_CORRELATION_ID:{job_id}\n\n{payload_body}",
            "mode": "chat",
            "attachments": attachments_payload,
            "reset": False,
        }

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(url, json=callback_payload, headers=headers)
                resp.raise_for_status()
            try:
                log.dual_log(tag="Worker:Callback:Success", message=f"Callback delivered for {job_id} (Files: {len(attachments_payload)})")
            except Exception:
                pass
        except Exception as e:
            try:
                log.dual_log(tag="Worker:Callback:Error", message=f"AnythingLLM callback failed: {str(e)}", level="ERROR")
            except Exception:
                pass


# Module-level singleton
_manager = UnifiedWorkerManager()


def get_manager() -> UnifiedWorkerManager:
    """Get the singleton unified worker manager."""
    return _manager
