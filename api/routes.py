# api/routes.py
"""Agent-native sync execution API.

POST /api/jobs: trigger a tool, hold the connection open until terminal state.
POST /api/jobs/{job_id}/resume: deliver HITL decision, hold until next terminal.
GET /api/jobs/{job_id}/status: return current DB state (for reconnection after disconnect).

The sync model eliminates SSE streaming. The LLM agent gets the final answer
or a precise failure message in a single HTTP round-trip.
"""
from fastapi import APIRouter, HTTPException, status, Request, Query
from typing import Dict, Any, Optional
import importlib
import json
import asyncio
from datetime import datetime, timezone

import config
from api.schemas import (
    SyncJobRequest, SyncJobResponse, JobCreateRequest, JobCreateResponse,
    JobStatusResponse, JobLogEntry, ResumeResponse, ResumeRequest,
    BackupMetricsResponse,
)
from tools.registry import REGISTRY
from utils.logger.core import get_dual_logger
from utils.id_generator import ULID
from database.writer import start_writer, enqueue_write
from database.connection import DatabaseManager, LogsDatabaseManager
from database.logs_writer import logs_enqueue_write
from database.diagnostics import get_queue_metrics
from bot.engine.worker import get_manager
from bot.engine.completion_registry import job_completion_registry
from utils.hitl_resolution import hitl_registry, VALID_DECISIONS

log = get_dual_logger(__name__)
router = APIRouter()

# Bounded concurrency: prevent resource exhaustion under WEB_CONCURRENCY=1.
# Ref: https://docs.python.org/3/library/asyncio-sync.html#asyncio.Semaphore
_sync_semaphore = asyncio.Semaphore(config.SYNC_MAX_CONCURRENT_JOBS)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_writer_running() -> None:
    try:
        start_writer()
    except Exception:
        log.dual_log(tag="API:Writer:Start", message="start_writer() failed (non-fatal)", payload={"action": "writer_start_failed"})


def get_session_id(request: Request) -> str:
    return "0"


async def _await_job_completion(job_id: str, request: Request) -> Dict[str, Any]:
    """Await the completion registry future with disconnect detection.

    Per Pushback 9: `await future` alone does NOT detect client disconnects.
    We race the future against `request.receive()` which blocks until an
    ASGI message arrives (including http.disconnect).

    Ref: https://marcelotryle.com/blog/2024/06/06/understanding-client-disconnection-in-fastapi
    """
    future = job_completion_registry.register(job_id)

    # Disconnect-detection task: awaits request.receive() which blocks until
    # the client sends another message OR disconnects.
    async def _wait_disconnect() -> None:
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return

    disconnect_task = asyncio.create_task(_wait_disconnect())
    future_task = asyncio.create_task(asyncio.wait_for(future, timeout=config.SYNC_API_TIMEOUT_SECONDS))

    try:
        done, pending = await asyncio.wait(
            {future_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        disconnect_task.cancel()
        future_task.cancel()
        raise

    # Cancel whichever task is still running.
    for p in pending:
        p.cancel()
        try:
            await p
        except (asyncio.CancelledError, Exception):
            pass

    if disconnect_task in done:
        # Client disconnected. The worker continues in the background;
        # terminal state is still committed to DB for later re-poll.
        log.dual_log(
            tag="API:Job:ClientDisconnect",
            message=f"Client disconnected while awaiting job {job_id}",
            level="WARNING",
            payload={"job_id": job_id},
        )
        raise HTTPException(
            status_code=499,  # Nginx convention for "Client Closed Request"
            detail={
                "job_id": job_id,
                "status": "UNKNOWN",
                "fallback": f"GET /api/jobs/{job_id}/status",
            },
        )

    if future_task in done:
        try:
            return future_task.result()
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail={
                    "job_id": job_id,
                    "status": "UNKNOWN",
                    "fallback": f"GET /api/jobs/{job_id}/status",
                    "message": f"Job did not complete within {config.SYNC_API_TIMEOUT_SECONDS}s",
                },
            )

    # Should be unreachable, but handle defensively.
    raise HTTPException(status_code=500, detail="Unexpected await state")


def _enqueue_job(tool_name: str, args: dict, request: Request, capture_lineage: bool = False) -> str:
    """Validate input, scan URLs, insert QUEUED row, start worker. Returns job_id."""
    meta = REGISTRY._tools.get(tool_name)
    if not meta:
        diagnostics = REGISTRY.diagnostic_list()
        diag_info = diagnostics.get(tool_name)
        if diag_info and diag_info.get("status") in ("FAILED", "REJECTED"):
            raise HTTPException(status_code=503, detail=f"Tool {tool_name} is currently degraded: {diag_info.get('error')}")
        raise HTTPException(status_code=404, detail="Tool not found")

    tool_cls = meta.get("cls")
    if tool_cls and hasattr(tool_cls, "INPUT_MODEL") and tool_cls.INPUT_MODEL:
        try:
            tool_cls.INPUT_MODEL.model_validate(args)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Validation failed: {str(e)}")

    from utils.security import scan_args_for_urls
    scan_args_for_urls(args)

    job_id = ULID.generate()
    session_id = get_session_id(request)

    # When capture_lineage is true, embed the flag in args_json so the worker
    # thread (which cannot receive the ContextVar from the API handler — see
    # the threading model diagram in the implementation plan) can read it
    # and instantiate the ActivityAccumulator.
    args_with_flag = dict(args)
    if capture_lineage:
        args_with_flag["_capture_lineage"] = True

    enqueue_write(
        "INSERT INTO jobs (job_id, session_id, tool_name, args_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, session_id, tool_name, json.dumps(args_with_flag), "QUEUED", now_iso(), now_iso())
    )

    _ensure_writer_running()
    get_manager().start()
    return job_id


@router.get("/manifest")
async def manifest():
    return {"tools": REGISTRY.schema_list()}


@router.post("/jobs", response_model=SyncJobResponse)
async def create_job_sync(req: SyncJobRequest, request: Request):
    """Trigger a tool and hold the connection until terminal state.

    Returns 200 with the terminal state (COMPLETED/FAILED/ABANDONED/PARTIAL/SKIPPED)
    or PAUSED_FOR_HITL. On timeout, returns 504 with a fallback pointer.
    On client disconnect, returns 499.
    """
    # Lineage capture requires staging mode to protect production memory.
    if req.capture_lineage and not getattr(config, "DATABASE_STAGING_ENABLED", False):
        raise HTTPException(
            status_code=403,
            detail="Lineage capture requires DATABASE_STAGING_ENABLED=true to protect production data."
        )

    async with _sync_semaphore:
        job_id = _enqueue_job(req.tool_name, req.args, request, capture_lineage=req.capture_lineage)

        log.dual_log(
            tag="API:Job:Sync:Enqueue",
            message=f"Sync job enqueued: {req.tool_name}",
            payload={"job_id": job_id, "tool_name": req.tool_name},
        )

        terminal = await _await_job_completion(job_id, request)

        # Cleanup the registry entry.
        job_completion_registry.cleanup(job_id)

        # When capture_lineage was requested, wrap the result per the
        # convention's LineageReport shape (§4.3.e):
        # {business_response_snapshot, lineage, summary}
        lineage_report = terminal.get("lineage")
        if req.capture_lineage and lineage_report is not None:
            result_payload = {
                "business_response_snapshot": terminal.get("result"),
                "lineage": lineage_report.get("lineage", []),
                "summary": lineage_report.get("summary", {}),
            }
        else:
            result_payload = terminal.get("result")

        return SyncJobResponse(
            job_id=terminal.get("job_id", job_id),
            status=terminal.get("status", "UNKNOWN"),
            result=result_payload,
            error=terminal.get("error"),
            tool_name=terminal.get("tool_name", req.tool_name),
            logs_pointer=f"GET /api/jobs/{job_id}/status" if terminal.get("status") in ("FAILED", "ABANDONED") else None,
            hitl_url=terminal.get("hitl_url"),
            hitl_reason=terminal.get("hitl_reason"),
        )


@router.post("/jobs/{job_id}/resume", response_model=SyncJobResponse)
async def resume_job(job_id: str, body: ResumeRequest, request: Request):
    """Resume a PAUSED_FOR_HITL job and hold until next terminal state.

    For INTERRUPTED/FAILED/PARTIAL: re-queues the job and awaits.
    """
    # Lineage capture on resume requires staging mode.
    if getattr(config, "DATABASE_STAGING_ENABLED", False) == False:
        # Read the original capture_lineage flag from args_json to determine
        # if the /resume caller wants lineage. For MVP, /resume does NOT
        # accept capture_lineage — it inherits from the original job.
        pass

    conn = DatabaseManager.get_read_connection()
    row = conn.execute("SELECT tool_name, status, args_json, resume_count FROM jobs WHERE job_id = ?", (job_id,)).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    current_status = row["status"]
    tool_name = row["tool_name"]

    TERMINAL_STATUSES = ("COMPLETED", "ABANDONED", "SKIPPED")
    if current_status == "CANCELLING":
        raise HTTPException(status_code=409, detail=f"Job is being cancelled (status={current_status}). Cannot resume.")
    if current_status in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"Job is terminal (status={current_status}). Cannot resume.")

    if current_status == "PAUSED_FOR_HITL":
        decision = body.decision if body.decision in VALID_DECISIONS else "proceed"
        delivered = hitl_registry.set_decision(job_id, decision)
        if not delivered:
            log.dual_log(tag="API:Job:Resume:HITLFallthrough", message=f"PAUSED_FOR_HITL job {job_id} has no waiting worker; falling through to re-queue.", level="WARNING", payload={"job_id": job_id})
        else:
            log.dual_log(tag="API:Job:Resume:HITL", message=f"HITL decision delivered for {job_id}", payload={"job_id": job_id, "decision": decision})

    if current_status != "PAUSED_FOR_HITL":
        # INTERRUPTED / FAILED / PARTIAL — re-queue via resume handler.
        try:
            args = json.loads(row["args_json"] or "{}")
        except Exception:
            args = {}

        try:
            mod = importlib.import_module(f"tools.{tool_name}.resume")
            handler = mod.ResumeHandler(job_id, args)
            report = handler.check_resume_state()
        except ImportError:
            raise HTTPException(status_code=501, detail=f"Tool '{tool_name}' does not implement resume handlers.")

        if not report.resumable:
            raise HTTPException(status_code=400, detail=report.message)

        resume_count = row["resume_count"] if row["resume_count"] is not None else 0
        if resume_count >= getattr(config, "MAX_RESUME_ATTEMPTS", 3):
            enqueue_write("UPDATE jobs SET status = 'FAILED', updated_at = ? WHERE job_id = ?", (now_iso(), job_id))
            raise HTTPException(status_code=400, detail="Maximum resume attempts exceeded (Poison pill protection).")

        enqueue_write(
            "UPDATE jobs SET status = 'QUEUED', resume_count = resume_count + 1, updated_at = ? WHERE job_id = ?",
            (now_iso(), job_id),
        )
        _ensure_writer_running()
        get_manager().start()

    async with _sync_semaphore:
        terminal = await _await_job_completion(job_id, request)
        job_completion_registry.cleanup(job_id)

        # Wrap result if lineage is present (same as create_job_sync).
        lineage_report = terminal.get("lineage")
        if lineage_report is not None:
            result_payload = {
                "business_response_snapshot": terminal.get("result"),
                "lineage": lineage_report.get("lineage", []),
                "summary": lineage_report.get("summary", {}),
            }
        else:
            result_payload = terminal.get("result")

        return SyncJobResponse(
            job_id=terminal.get("job_id", job_id),
            status=terminal.get("status", "UNKNOWN"),
            result=result_payload,
            error=terminal.get("error"),
            tool_name=terminal.get("tool_name", tool_name),
            logs_pointer=f"GET /api/jobs/{job_id}/status" if terminal.get("status") in ("FAILED", "ABANDONED") else None,
            hitl_url=terminal.get("hitl_url"),
            hitl_reason=terminal.get("hitl_reason"),
        )


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str, request: Request):
    """Return current DB state for reconnection after a disconnect."""
    try:
        conn = DatabaseManager.get_read_connection()
        row = conn.execute(
            "SELECT job_id, tool_name, status, args_json, result_json, created_at, updated_at FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    except Exception:
        row = None

    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    status_val = row["status"]

    job_logs = []
    try:
        conn = LogsDatabaseManager.get_read_connection()
        rows = conn.execute(
            "SELECT timestamp, level, tag, status_state, message FROM logs WHERE job_id = ? ORDER BY timestamp",
            (job_id,),
        ).fetchall()
        for r in rows:
            job_logs.append(JobLogEntry(timestamp=r["timestamp"], level=r["level"], tag=r["tag"], status_state=r["status_state"], message=r["message"]))
    except Exception:
        pass

    final_payload = None
    try:
        if row and row["result_json"]:
            parsed_result = json.loads(row["result_json"])
            final_payload = parsed_result.get("result", parsed_result)
    except Exception:
        final_payload = {"raw": row["result_json"] if row else None}

    return JobStatusResponse(job_id=job_id, status=status_val, job_logs=job_logs, final_payload=final_payload)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_job(job_id: str, request: Request):
    """Request job cancellation."""
    flag = None
    try:
        mgr = get_manager()
        flag = mgr.cancellation_flags.get(job_id)
    except Exception:
        pass

    ts = now_iso()
    try:
        enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ("CANCELLING", ts, job_id))
        logs_enqueue_write(
            "INSERT INTO logs (id, job_id, tag, level, status_state, message, payload_json, event_id, error_json, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ULID.generate(), job_id, "system", "INFO", "CANCELLING", "Cancellation requested via API", None, ULID.generate(), None, ts),
        )
    except Exception:
        pass

    log.dual_log(tag="API:Job:Cancel", message=f"Cancel requested for job {job_id}", payload={"job_id": job_id, "had_flag": bool(flag)})

    if flag:
        flag.set()
    return {"job_id": job_id, "status": "CANCELLING"}


@router.get("/diagnostics")
async def diagnostics():
    from database.diagnostics import get_queue_metrics
    metrics = get_queue_metrics()
    try:
        from bot.engine.worker import get_manager
        mgr = get_manager()
        active_jobs = len(mgr.cancellation_flags) if mgr else 0
    except Exception:
        active_jobs = 0
    metrics["active_jobs"] = active_jobs
    return metrics


# Deprecated alias — returns 202 immediately (no sync await).
# Kept for backward compat; new code should use POST /api/jobs.
@router.post("/tools/{tool_name}", response_model=JobCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def enqueue_tool(tool_name: str, req: JobCreateRequest, request: Request):
    job_id = _enqueue_job(tool_name, req.args, request, capture_lineage=False)
    return {"job_id": job_id, "status": "QUEUED"}


@router.get("/backup/status", response_model=BackupMetricsResponse)
async def get_backup_status():
    from utils.startup import _global_sync_engine
    if not _global_sync_engine:
        raise HTTPException(status_code=503, detail="Backup engine is not active")
    from database.backup.observability.metrics import BackupMetricsCollector
    metrics = BackupMetricsCollector.get_metrics(_global_sync_engine)
    return BackupMetricsResponse(**metrics)


@router.post("/backup/export")
async def trigger_export(mode: str = Query("delta")):
    from database.backup.settings import BackupSettings
    from database.backup.runner import BackupRunner
    from fastapi import BackgroundTasks
    settings = BackupSettings()
    if not settings.cloud.enabled:
        raise HTTPException(status_code=400, detail="Backups are completely disabled")
    job_id = str(ULID.generate())
    # NOTE: BackgroundTasks must be a parameter, not imported. Fix below.
    return await _trigger_export_impl(job_id, mode)


async def _trigger_export_impl(job_id: str, mode: str):
    from fastapi import BackgroundTasks
    from database.backup.runner import BackupRunner
    # This is a simplified path — the original used BackgroundTasks parameter.
    # For now, run synchronously in a thread to avoid blocking the event loop.
    import asyncio
    from database.backup.runner import BackupRunner
    asyncio.create_task(asyncio.to_thread(BackupRunner.run, mode=mode, trigger_type="manual", manual_job_id=job_id))
    return {"status": "EXPORT_QUEUED", "job_id": job_id}


@router.post("/backup/restore")
async def trigger_restore():
    import asyncio
    from database.backup.runner import BackupRunner
    job_id = str(ULID.generate())
    asyncio.create_task(asyncio.to_thread(BackupRunner.restore, manual_job_id=job_id))
    return {"status": "RESTORE_QUEUED", "job_id": job_id}


