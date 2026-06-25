# bot/engine/worker.py
"""Unified Worker Manager with crash recovery and caller-level locking.

The worker polls the jobs table, spawns execution threads for each job,
and persists the terminal state directly to the database. The sync API
await-future pattern is bridged via bot.engine.completion_registry.

State machine (validated by bot.engine.health.InlineHealthChecker):
  QUEUED -> RUNNING -> COMPLETED | FAILED | INTERRUPTED | ABANDONED |
                       PAUSED_FOR_HITL | PARTIAL | SKIPPED | CANCELLING
  INTERRUPTED -> RUNNING (retry)
  PAUSED_FOR_HITL -> RUNNING | CANCELLING | SKIPPED
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
from database.writer import enqueue_write, WriteReceipt
from utils.id_generator import ULID
from utils.context_helpers import spawn_thread_with_context
from tools.registry import REGISTRY
from tools.base import ToolError, ToolValidationError
from bot.engine.state_guard import InlineStateGuard, StateTransitionViolation, TERMINAL_STATUSES
from bot.engine.completion_registry import job_completion_registry

log = get_dual_logger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UnifiedWorkerManager:
    """Poll jobs table and execute tools directly."""

    def __init__(
        self,
        poll_interval: float = 1.0,
        state_guard: InlineStateGuard | None = None,
    ):
        import collections
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_jobs: Dict[str, threading.Thread] = {}
        self._system_errors = collections.defaultdict(int)
        self.cancellation_flags: Dict[str, threading.Event] = {}
        # Inline state guard — validates state transitions and result
        # payloads at runtime. Exceptions propagate (fail fast).
        self._state_guard = state_guard or InlineStateGuard()

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

                # Validate QUEUED -> RUNNING transition BEFORE writing.
                self._state_guard.check_state_transition(job_id, status, "RUNNING")

                ts = now_iso()
                enqueue_write(
                    "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                    ("RUNNING", ts, job_id),
                )

                if status == "INTERRUPTED":
                    recovery_msg = (
                        "RESUMPTION NOTICE: The system recovered from an interruption. "
                        "The browser has been restarted at Google. Verify your location before "
                        "proceeding. Consult job_items to review completed steps."
                    )
                    log.dual_log(tag="Worker:Job:Recovery", message="Recovered interrupted job", payload={"job_id": job_id, "recovery_notice": recovery_msg})

                flag = threading.Event()
                self.cancellation_flags[job_id] = flag

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

        Exception handling contract:
          - ToolError subclasses (from tools) -> FAILED with error message
          - StateTransitionViolation (from health checker) -> FAILED with violation
          - Other Exception -> 3-strike crash recovery (INTERRUPTED / ABANDONED)

        The completion registry is resolved on EVERY terminal state, including
        FAILED and ABANDONED, so the sync API never hangs.
        """
        # --- Activity-Driven Observability ---
        # Read the capture_lineage flag from args_json. The flag was embedded
        # by _enqueue_job in api/routes.py because the ContextVar cannot
        # cross the API-handler → polling-thread boundary (the polling thread
        # is spawned via plain threading.Thread at worker.py:63, which does
        # NOT copy context).
        capture_lineage = args.pop("_capture_lineage", False) and getattr(config, "DATABASE_STAGING_ENABLED", False)

        accumulator = None
        token = None
        if capture_lineage:
            from utils.observability.accumulator import ActivityAccumulator
            from utils.observability.context import bind_accumulator
            max_activities = getattr(config, "LINEAGE_MAX_ACTIVITIES", 1000)
            max_chars = getattr(config, "LINEAGE_MAX_STRING_CHARS", 50000)
            accumulator = ActivityAccumulator(
                job_id, tool_name,
                max_activities=max_activities,
                max_chars=max_chars,
            )
            token = bind_accumulator(accumulator)
        # Initialize res to None so the attachments check below is safe
        # (per Pushback 6: if tool_instance is None, res is never assigned).
        res = None
        try:
            async def telemetry_cb(update):
                pass

            tool_instance = REGISTRY.create_tool_instance(tool_name)
            if not tool_instance:
                result = {"status": "FAILED", "error": f"Tool {tool_name} not found", "result": None}
            else:
                from bot.engine.tool_runner import run_tool_safely
                res = asyncio.run(run_tool_safely(tool_instance, args, telemetry_cb, job_id=job_id, session_id=session_id, cancellation_flag=cancellation_flag))
                # res.success is always True here (run_tool_safely doesn't catch
                # exceptions anymore). If it's False, something is very wrong.
                if res is None:
                    result = {"status": "FAILED", "error": "Tool returned None ToolResult", "result": None}
                elif res.success:
                    # Parse output: try JSON, fall back to raw string.
                    # Per Pushback 8: handle None output explicitly.
                    if res.output is None:
                        result = {"status": "COMPLETED", "result": ""}
                    else:
                        try:
                            parsed = json.loads(res.output)
                            result = {"status": "COMPLETED", "result": parsed}
                        except Exception:
                            result = {"status": "COMPLETED", "result": res.output}
                else:
                    # This branch is theoretically unreachable now (run_tool_safely
                    # doesn't return success=False), but we handle it defensively.
                    result = {"status": "FAILED", "error": res.output, "result": None}

            # Normalize result to a plain dict.
            # Per Pushback 8: dict(None) raises TypeError — handle explicitly.
            if isinstance(result, dict):
                normal = result
            elif result is None:
                normal = {"status": "FAILED", "error": "Tool returned None", "result": None}
            else:
                try:
                    import sqlite3 as _sqlite3
                    if isinstance(result, _sqlite3.Row):
                        normal = dict(result)
                    else:
                        normal = dict(result)
                except Exception:
                    normal = {"status": "FAILED", "error": str(result), "result": None}

            # Attach file paths if res was assigned (per Pushback 6).
            if res is not None and getattr(res, "attachment_paths", None):
                normal["attachment_paths"] = res.attachment_paths

            status_str = normal.get("status", "FAILED")

            # Validate terminal result BEFORE committing.
            # Per Pushback 4: validator exceptions propagate (fail fast).
            self._state_guard.check_terminal_result(job_id, normal)

            # Validate RUNNING -> terminal transition.
            self._state_guard.check_state_transition(job_id, "RUNNING", status_str)

            # Serialize. Exception messages are NEVER truncated — they are
            # the LLM's diagnostic lifeline. SQLite TEXT has no length limit.
            payload_json = json.dumps(normal, ensure_ascii=False, default=str)

            # Commit terminal state with tracked WriteReceipt.
            # Per Pushback 7: WriteReceipt.wait() returns True on BOTH success
            # and rejection; check receipt.error to distinguish.
            receipt = enqueue_write(
                "UPDATE jobs SET status = ?, result_json = ?, updated_at = ? WHERE job_id = ?",
                (status_str, payload_json, now_iso(), job_id),
                track=True,
            )
            if receipt is not None:
                committed = receipt.wait(timeout=45.0)
                if not committed:
                    # Timeout — DB writer stuck. Resolve future with FAILED
                    # so the API doesn't hang, but log critically.
                    log.dual_log(
                        tag="Worker:Job:CommitTimeout",
                        message=f"Terminal state commit timed out for {job_id}",
                        level="CRITICAL",
                        payload={"job_id": job_id, "status": status_str},
                    )
                    normal = {"status": "FAILED", "error": "DB commit timeout", "result": None}
                    status_str = "FAILED"
                elif receipt.error is not None:
                    # Write was rejected (e.g., queue full, FK violation).
                    log.dual_log(
                        tag="Worker:Job:CommitRejected",
                        message=f"Terminal state commit rejected for {job_id}",
                        level="CRITICAL",
                        payload={"job_id": job_id, "error": str(receipt.error)},
                    )
                    normal = {"status": "FAILED", "error": f"DB commit failed: {receipt.error}", "result": None}
                    status_str = "FAILED"

            # Log the terminal state transition.
            from database.logs_writer import logs_enqueue_write
            terminal_log_entry = {
                "id": ULID.generate(),
                "job_id": job_id,
                "tag": "Worker:Job:Terminal",
                "level": "INFO",
                "status_state": status_str,
                "message": f"Job reached terminal state: {status_str}",
                "payload_json": json.dumps({"status": status_str, "tool": tool_name, "output_len": len(str(normal.get("result", "")))}),
                "event_id": ULID.generate(),
                "error_json": None,
                "timestamp": now_iso(),
            }
            logs_enqueue_write(
                "INSERT INTO logs (id, job_id, tag, level, status_state, message, payload_json, event_id, error_json, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (terminal_log_entry["id"], terminal_log_entry["job_id"], terminal_log_entry["tag"],
                 terminal_log_entry["level"], terminal_log_entry["status_state"], terminal_log_entry["message"],
                 terminal_log_entry["payload_json"], terminal_log_entry["event_id"], terminal_log_entry["error_json"],
                 terminal_log_entry["timestamp"]),
            )

            # Validate the log entry's payload signature.
            # Per Pushback 2: real I/O inspection, not pure assertion.
            try:
                self._state_guard.check_log_payload(job_id, terminal_log_entry)
            except StateTransitionViolation:
                # Log payload validation failure is non-fatal (the job is
                # already terminal). Log at ERROR but don't change the status.
                log.dual_log(
                    tag="Worker:Job:LogPayloadInvalid",
                    message=f"Terminal log payload validation failed for {job_id}",
                    level="ERROR",
                    payload={"job_id": job_id},
                )

            # Reset errors on success.
            if job_id in self._system_errors:
                del self._system_errors[job_id]

            # --- Finalize the lineage report (if capture was enabled) ---
            lineage_report = None
            if accumulator is not None:
                lineage_report = accumulator.finalize(
                    business_response=normal.get("result")
                )
                # Convert to plain dict for JSON serialization across the
                # completion registry boundary.
                lineage_report = lineage_report.model_dump()

            # Resolve the completion registry future so the sync API unblocks.
            # This is the CRITICAL line that was missing in the original plan.
            job_completion_registry.resolve(job_id, {
                "job_id": job_id,
                "status": status_str,
                "result": normal.get("result"),
                "error": normal.get("error"),
                "tool_name": tool_name,
                "lineage": lineage_report,
            })

        except ToolValidationError as e:
            # Input validation failure — terminal FAILED, no retry.
            log.dual_log(tag="Worker:Job:ValidationError", message=f"Job {job_id} validation failed", level="WARNING", exc_info=e, payload={"job_id": job_id, "error": str(e)})
            self._commit_failed(job_id, tool_name, str(e), accumulator)

        except ToolError as e:
            # Tool execution failure — terminal FAILED, no retry.
            log.dual_log(tag="Worker:Job:ToolError", message=f"Job {job_id} tool error", level="ERROR", exc_info=e, payload={"job_id": job_id, "error": str(e)})
            self._commit_failed(job_id, tool_name, str(e), accumulator)

        except StateTransitionViolation as e:
            # Health checker caught an invariant breach — terminal FAILED.
            # Per Pushback 4: fail fast, do not continue.
            log.dual_log(tag="Worker:Job:StateViolation", message=f"Job {job_id} state transition violation", level="CRITICAL", exc_info=e, payload={"job_id": job_id, "error": str(e)})
            self._commit_failed(job_id, tool_name, str(e), accumulator)

        except Exception as e:
            # Unhandled crash — 3-strike crash recovery.
            # The dead "PAUSED_FOR_HITL:" string-match is GONE (per Pushback 5).
            # HITL pauses are handled inside tools/scraper/hitl.py which resolves
            # the completion registry BEFORE blocking, so the worker never sees
            # an exception for HITL.
            self._system_errors[job_id] += 1
            if self._system_errors[job_id] >= 3:
                log.dual_log(tag="Worker:Job:Abandoned", message=f"Job {job_id} ABANDONED after 3 consecutive system errors", level="CRITICAL", payload={"job_id": job_id, "error": str(e)})
                self._commit_status(job_id, tool_name, "ABANDONED", str(e), accumulator)
                del self._system_errors[job_id]
            else:
                log.dual_log(tag="Worker:Job:Crashed", message=f"Job {job_id} crashed", level="ERROR", exc_info=e, payload={"job_id": job_id, "attempt": self._system_errors[job_id], "error": str(e)})
                time.sleep(10)
                self._commit_status(job_id, tool_name, "INTERRUPTED", str(e), accumulator)

        finally:
            # Unbind the accumulator to prevent context leaks across jobs.
            if token is not None:
                from utils.observability.context import unbind_accumulator
                unbind_accumulator(token)

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
    def _commit_failed(self, job_id: str, tool_name: str, error_msg: str, accumulator=None) -> None:
        """Commit FAILED status and resolve the completion registry."""
        normal = {"status": "FAILED", "error": error_msg, "result": None}
        payload_json = json.dumps(normal, ensure_ascii=False, default=str)
        receipt = enqueue_write(
            "UPDATE jobs SET status = ?, result_json = ?, updated_at = ? WHERE job_id = ?",
            ("FAILED", payload_json, now_iso(), job_id),
            track=True,
        )
        if receipt is not None:
            receipt.wait(timeout=45.0)
        from database.logs_writer import logs_enqueue_write
        logs_enqueue_write(
            "INSERT INTO logs (id, job_id, tag, level, status_state, message, payload_json, event_id, error_json, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ULID.generate(), job_id, "Worker:Job:Failed", "ERROR", "FAILED",
              f"Job failed: {error_msg[:200]}",
              json.dumps({"error_len": len(error_msg)}),
              ULID.generate(), json.dumps({"error": error_msg}, ensure_ascii=False), now_iso()),
        )

        lineage_report = None
        if accumulator is not None:
            lineage_report = accumulator.finalize(business_response=None).model_dump()

        job_completion_registry.resolve(job_id, {
            "job_id": job_id,
            "status": "FAILED",
            "result": None,
            "error": error_msg,
            "tool_name": tool_name,
            "lineage": lineage_report,
        })

    def _commit_status(self, job_id: str, tool_name: str, status: str, error_msg: str, accumulator=None) -> None:
        """Commit a non-FAILED terminal status (ABANDONED / INTERRUPTED) and resolve."""
        normal = {"status": status, "error": error_msg, "result": None}
        payload_json = json.dumps(normal, ensure_ascii=False, default=str)
        enqueue_write(
            "UPDATE jobs SET status = ?, result_json = ?, updated_at = ? WHERE job_id = ?",
            (status, payload_json, now_iso(), job_id),
        )

        lineage_report = None
        if accumulator is not None:
            lineage_report = accumulator.finalize(business_response=None).model_dump()

        job_completion_registry.resolve(job_id, {
            "job_id": job_id,
            "status": status,
            "result": None,
            "error": error_msg,
            "tool_name": tool_name,
            "lineage": lineage_report,
        })


_manager = UnifiedWorkerManager()


def get_manager() -> UnifiedWorkerManager:
    """Get the singleton unified worker manager."""
    return _manager
