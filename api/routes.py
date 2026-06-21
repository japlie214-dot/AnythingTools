# api/routes.py
from fastapi import APIRouter, HTTPException, status, Request, BackgroundTasks, Query, Header
from typing import Dict, Any, Optional
from typing_extensions import Annotated
import threading
import asyncio
import importlib
import json
from datetime import datetime, timezone

import config
from api.schemas import JobCreateRequest, JobCreateResponse, JobStatusResponse, JobLogEntry, ResumeResponse, BackupMetricsResponse, ResumeRequest
from tools.registry import REGISTRY
from utils.logger.core import get_dual_logger
from utils.id_generator import ULID
from database.writer import start_writer, enqueue_write
from database.connection import DatabaseManager, LogsDatabaseManager
from database.logs_writer import logs_enqueue_write
from database.diagnostics import get_queue_metrics
from bot.engine.worker import get_manager
from utils.hitl_resolution import hitl_registry, VALID_DECISIONS
from fastapi.responses import StreamingResponse
from utils.sse import get_global_broker, StreamEndEvent
from api.schemas import HealthCheckRequest, HealthCheckResponse
from pydantic import ValidationError

log = get_dual_logger(__name__)
router = APIRouter()

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_writer_running() -> None:
    try:
        start_writer()
    except Exception:
        log.dual_log(tag="API:Writer:Start", message="start_writer() failed (non-fatal)", payload={"action": "writer_start_failed"})


def get_session_id(request: Request) -> str:
    return "0"  # Hardcoded fallback for legacy DB constraints, or read form input


@router.get("/manifest")
async def manifest():
    return {"tools": REGISTRY.schema_list()}


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str, request: Request):
    # Attempt to read from the DB
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

    # Load job logs from logs.db using LogsDatabaseManager
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

    # Attempt to read final payload directly from the operational jobs table
    final_payload = None
    try:
        if row and row["result_json"]:
            parsed_result = json.loads(row["result_json"])
            # The worker wraps output in {"status": ..., "result": ...}
            final_payload = parsed_result.get("result", parsed_result)
            
    except Exception:
        final_payload = {"raw": row["result_json"] if row else None}

    return JobStatusResponse(job_id=job_id, status=status_val, job_logs=job_logs, final_payload=final_payload)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_job(job_id: str, request: Request):
    """Request job cancellation. Marks job as CANCELLING and sets cancellation flag if running."""
    flag = None
    try:
        mgr = get_manager()
        flag = mgr.cancellation_flags.get(job_id)
    except Exception:
        pass

    # Persist cancellation request immediately
    ts = now_iso()
    try:
        enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ("CANCELLING", ts, job_id))
        # Write cancellation log to logs.db
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
    else:
        # Job not actively running yet; we marked it in DB and manager will honor it.
        return {"job_id": job_id, "status": "CANCELLING"}

@router.get("/diagnostics")
async def diagnostics():
    """Return internal metrics for observability."""
    from database.diagnostics import get_queue_metrics
    metrics = get_queue_metrics()
    # Optionally add jobs active count
    try:
        from bot.engine.worker import get_manager
        mgr = get_manager()
        active_jobs = len(mgr.cancellation_flags) if mgr else 0
    except Exception:
        active_jobs = 0
    metrics["active_jobs"] = active_jobs
    return metrics


@router.get("/jobs/{job_id}/stream")
async def stream_job_events(
    job_id: str,
    request: Request,
    last_event_id: Annotated[Optional[str], Header(alias="Last-Event-ID")] = None,
):
    """SSE stream for a job's execution events.

    Emits 4 phase events: started, running, paused, completed. Ref:
    https://fastapi.tiangolo.com/tutorial/server-sent-events/
    https://html.spec.whatwg.org/multipage/server-sent-events.html
    """
    try:
        conn = DatabaseManager.get_read_connection()
        row = conn.execute("SELECT job_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    except Exception:
        row = None
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    from fastapi.sse import EventSourceResponse
    return EventSourceResponse(stream_job(job_id, last_event_id))


@router.post("/jobs/{job_id}/resume", response_model=ResumeResponse)
async def resume_job(job_id: str, body: ResumeRequest = ResumeRequest()):
    """Resume a PAUSED_FOR_HITL, INTERRUPTED, FAILED, or PARTIAL job.

    For INTERRUPTED/FAILED/PARTIAL: re-queues the job and starts the worker.
    Bypasses MAX_RESUME_ATTEMPTS for HITL resumes (operator-driven, not doom loop).
    """
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
            return ResumeResponse(
                job_id=job_id,
                tool_name=tool_name,
                status="RUNNING",
                items_completed=0,
                items_pending=0,
                message=f"HITL decision '{decision}' delivered. Reconnect to /stream.",
                details={"decision": decision, "resume_path": "hitl"},
            )

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

    if current_status != "PAUSED_FOR_HITL":
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

    log.dual_log(
        tag="API:Job:Resume",
        message=f"Job {job_id} queued for resumption.",
        payload={"job_id": job_id, "tool": tool_name, "report": report.__dict__},
    )

    return ResumeResponse(
        job_id=job_id,
        tool_name=report.tool_name,
        status="QUEUED",
        items_completed=report.items_completed,
        items_pending=report.items_pending,
        message=report.message,
        details=report.details
    )

@router.post("/tools/{tool_name}", response_model=JobCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def enqueue_tool(tool_name: str, req: JobCreateRequest, request: Request):
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
            tool_cls.INPUT_MODEL.model_validate(req.args)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Validation failed: {str(e)}")

    from utils.security import scan_args_for_urls
    scan_args_for_urls(req.args)

    job_id = ULID.generate()
    session_id = get_session_id(request)
    
    enqueue_write(
        "INSERT INTO jobs (job_id, session_id, tool_name, args_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, session_id, tool_name, json.dumps(req.args), "QUEUED", now_iso(), now_iso())
    )
    
    _ensure_writer_running()
    get_manager().start()
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
async def trigger_export(background_tasks: BackgroundTasks, mode: str = Query("delta")):
    from database.backup.settings import BackupSettings
    from database.backup.runner import BackupRunner
    settings = BackupSettings()
    if not settings.cloud.enabled:
        raise HTTPException(status_code=400, detail="Backups are completely disabled")
        
    job_id = str(ULID.generate())
    background_tasks.add_task(BackupRunner.run, mode=mode, trigger_type="manual", manual_job_id=job_id)
    return {"status": "EXPORT_QUEUED", "job_id": job_id}

@router.post("/backup/restore")
async def trigger_restore(background_tasks: BackgroundTasks):
    from database.backup.runner import BackupRunner
    job_id = str(ULID.generate())
    background_tasks.add_task(BackupRunner.restore, manual_job_id=job_id)
    return {"status": "RESTORE_QUEUED", "job_id": job_id}

# ─── SSE Streaming Endpoint ─────────────────────────────────────────────
# Refs:
#   https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse
#   https://html.spec.whatwg.org/multipage/server-sent-events.html
#   https://www.starlette.io/requests/#disconnecting

@router.get("/jobs/{job_id}/stream")
async def stream_job_events(job_id: str, request: Request):
    """Stream real-time SSE events for a job.

    Emits event types:
      - job.status_changed: when the job's status transitions
      - log.appended: for each new log entry in logs.db
      - tool.progress: intermediate progress from the tool
      - stream.end: when the job reaches terminal state or client disconnects

    The stream uses the SSE wire format (text/event-stream) with:
      - 15-second keep-alive comment lines to prevent proxy timeouts
      - Cache-Control: no-cache to prevent intermediary caching
      - X-Accel-Buffering: no to disable nginx buffering

    Ref: https://html.spec.whatwg.org/multipage/server-sent-events.html#authoring-notes
    """
    broker = get_global_broker()
    await broker.start_tailer()

    # Check subscriber limit
    async with broker._lock:
        current_count = len(broker._subscribers.get(job_id, set()))
    if current_count >= config.SSE_MAX_SUBSCRIBERS_PER_JOB:
        raise HTTPException(status_code=429, detail="Too many SSE subscribers for this job")

    queue = await broker.subscribe(job_id)

    async def event_generator():
        """Async generator yielding SSE-formatted events.

        Each event is serialized as ``data: <json>\n\n`` per the HTML spec.
        Comment lines (``: ping\n\n``) are sent every 15 seconds when idle.
        """
        try:
            while True:
                # Check for client disconnect
                # Ref: https://www.starlette.io/requests/#disconnecting
                if await request.is_disconnected():
                    break

                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=config.SSE_KEEPALIVE_INTERVAL_SECONDS
                    )
                    # Serialize event to SSE wire format
                    # Ref: https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream
                    data = event.to_sse_data()
                    yield f"event: {event.event_type}\ndata: {data}\n\n".encode("utf-8")

                    # If terminal event, close the stream
                    if isinstance(event, StreamEndEvent):
                        break

                except asyncio.TimeoutError:
                    # Send keep-alive comment to prevent proxy idle-timeout close.
                    # Ref: https://html.spec.whatwg.org/multipage/server-sent-events.html#authoring-notes
                    yield b": ping\n\n"

        finally:
            await broker.unsubscribe(job_id, queue, reason="stream_closed")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ─── Health Check Endpoint ──────────────────────────────────────────────

@router.post("/health-check/{tool_name}", response_model=HealthCheckResponse)
async def health_check_tool(tool_name: str, request: Request):
    """Enqueue a health-check job for a tool and return a stream URL.

    This endpoint:
      1. Validates DATABASE_STAGING_ENABLED is True (health checks must
         not write to production data).
      2. Validates the tool exists and implements health_check_payload().
      3. Enqueues a real job using the tool's happy_path_args.
      4. Returns the job_id and a stream URL for SSE consumption.

    The client subscribes to the stream URL to receive real-time
    execution events. The stream closes when the job reaches terminal
    state (COMPLETED or FAILED).

    Health checks run both happy and error paths sequentially. The
    happy path job is enqueued first; the error path can be triggered
    by calling the endpoint again with ?path=error query param.
    """
    # Gate: staging must be enabled
    if not getattr(config, "DATABASE_STAGING_ENABLED", False):
        raise HTTPException(
            status_code=403,
            detail="Health checks require DATABASE_STAGING_ENABLED=true to protect production data."
        )

    # Validate tool exists
    meta = REGISTRY._tools.get(tool_name)
    if not meta:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    tool_cls = meta.get("cls")
    if not tool_cls or not hasattr(tool_cls, "health_check_payload"):
        raise HTTPException(
            status_code=501,
            detail=f"Tool '{tool_name}' does not implement health_check_payload()."
        )

    # Get health check payload (instantiate tool to call the method)
    tool_instance = REGISTRY.create_tool_instance(tool_name)
    if not tool_instance:
        raise HTTPException(status_code=500, detail=f"Failed to instantiate tool '{tool_name}'")

    payload = tool_instance.health_check_payload()

    # Determine which path to run (default: happy)
    path = request.query_params.get("path", "happy")
    if path == "error":
        args = payload.error_path_args
        expected_status = payload.expected_error_status
    else:
        args = payload.happy_path_args
        expected_status = payload.expected_happy_status

    # Enqueue the job
    job_id = ULID.generate()
    session_id = get_session_id(request)

    enqueue_write(
        "INSERT INTO jobs (job_id, session_id, tool_name, args_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, session_id, tool_name, json.dumps(args), "QUEUED", now_iso(), now_iso())
    )

    _ensure_writer_running()
    get_manager().start()

    # Build stream URL
    base_url = str(request.base_url).rstrip("/")
    stream_url = f"{base_url}/api/jobs/{job_id}/stream"

    log.dual_log(
        tag="HealthCheck:Enqueued",
        message=f"Health check enqueued for {tool_name} (path={path})",
        payload={
            "who": f"health_check_endpoint",
            "what": "enqueue_health_check",
            "when": now_iso(),
            "where": f"tool:{tool_name}",
            "why": f"validate_{path}_path",
            "how": "POST /api/health-check/{tool_name}",
            "job_id": job_id,
            "tool_name": tool_name,
            "path": path,
            "expected_status": expected_status,
            "timeout_seconds": payload.timeout_seconds or config.HEALTH_CHECK_TIMEOUT_SECONDS,
        },
    )

    return HealthCheckResponse(
        job_id=job_id,
        tool_name=tool_name,
        stream_url=stream_url,
        timeout_seconds=payload.timeout_seconds or config.HEALTH_CHECK_TIMEOUT_SECONDS,
    )
