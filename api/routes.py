# api/routes.py
from fastapi import APIRouter, HTTPException, status, Request, Depends, BackgroundTasks, Query
from typing import Dict, Any
import threading
import asyncio
import importlib
import json
from datetime import datetime, timezone

import config
from api.schemas import JobCreateRequest, JobCreateResponse, JobStatusResponse, JobLogEntry, BackupStatusResponse, ExportQueuedResponse, RestoreQueuedResponse
from tools.registry import REGISTRY
from utils.logger.core import get_dual_logger
from utils.id_generator import ULID
from database.writer import start_writer, enqueue_write
from database.connection import DatabaseManager, LogsDatabaseManager
from database.logs_writer import logs_enqueue_write
from utils.artifact_manager import artifact_url_from_request
from bot.engine.worker import get_manager
from pydantic import ValidationError

# Backup imports
from database.backup.config import BackupConfig
from database.backup.runner import BackupRunner
from utils.browser_lock import browser_lock

# Security imports
from utils.security import scan_args_for_urls

log = get_dual_logger(__name__)
router = APIRouter()

# Durable state exclusively in the database; in-memory mirrors removed.


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_writer_running() -> None:
    try:
        start_writer()
    except Exception:
        log.dual_log(tag="API:Writer:Start", message="start_writer() failed (non-fatal)")


def get_session_id(request: Request) -> str:
    return "0"  # Hardcoded fallback for legacy DB constraints


@router.post("/tools/{tool_name}", response_model=JobCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def enqueue_tool(tool_name: str, req: JobCreateRequest, request: Request):
    session_id = "0"  # Hardcoded fallback for legacy DB constraints
    # Refresh registry so code changes are visible
    REGISTRY.load_all()
    meta = REGISTRY._tools.get(tool_name)
    if not meta:
        diagnostics = REGISTRY.diagnostic_list()
        diag_info = diagnostics.get(tool_name)
        
        if diag_info and diag_info.get("status") in ("FAILED", "REJECTED"):
            reason = diag_info.get("error", "Unknown error")
            raise HTTPException(status_code=503, detail=f"Tool failed to load: {reason}")
        
        raise HTTPException(status_code=404, detail="Tool not found")

    # Circuit breaker for browser-bound tools
    if tool_name in ["scraper", "browser_task"]:
        from utils.browser_daemon import daemon_manager, BrowserStatus
        if daemon_manager.status != BrowserStatus.READY:
            raise HTTPException(
                status_code=503, 
                detail=f"Browser environment is currently {daemon_manager.status.value}. Tool unavailable."
            )

    # Input validation using optional INPUT_MODEL exported by the tool module
    InputModel = None
    try:
        module = importlib.import_module(meta.get("module"))
        InputModel = getattr(module, "INPUT_MODEL", None)
    except Exception:
        InputModel = None

    try:
        if InputModel is not None:
            validated_args = InputModel.model_validate(req.args)
            args = validated_args.model_dump()
        else:
            args = req.args
        
        if req.client_metadata:
            args["_client_metadata"] = req.client_metadata
    except ValidationError as e:
        # Return 422 with structured validation errors
        error_details = []
        for error in e.errors():
            error_details.append({
                "field": " -> ".join(str(loc) for loc in error.get("loc", [])),
                "message": error.get("msg", ""),
                "type": error.get("type", ""),
                "input": str(error.get("input", "N/A"))[:100]
            })
            
        expected_schema = None
        if InputModel is not None:
            if hasattr(InputModel, "model_json_schema"):
                expected_schema = InputModel.model_json_schema()
            elif hasattr(InputModel, "schema"):
                expected_schema = InputModel.schema()

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": f"Validation failed for tool '{tool_name}'",
                "errors": error_details,
                "expected_schema": expected_schema
            }
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Input validation failed: {e}")

    # SSRF / URL validation for any URL-like arguments
    try:
        scan_args_for_urls(args)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Unsafe URL in arguments: {e}")

    job_id = ULID.generate()
    created = now_iso()
    
    # Persist job record (writer will serialize DB writes)
    # Log system: intent and pre-execution state
    log.dual_log(
        tag="API:Job:Create",
        message=f"Enqueueing job for tool '{tool_name}'",
        payload={"tool": tool_name, "args": args, "job_id": job_id, "session_id": session_id, "status": "QUEUED"}
    )
    enqueue_write(
        "INSERT INTO jobs (job_id, session_id, tool_name, args_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, session_id, tool_name, json.dumps(args), "QUEUED", created, created),
    )
    log.dual_log(tag="API:Job:Persist", message=f"Job {job_id} persisted", payload={"job_id": job_id})

    # Ensure background writer is running
    _ensure_writer_running()

    # Start the persistent manager so it will claim and run queued jobs
    try:
        mgr = get_manager()
        mgr.start()
    except Exception as e:
        log.dual_log(tag="API:Worker:Start", message=f"Failed to start worker manager: {e}", level="WARNING", exc_info=e)

    return {"job_id": job_id, "status": "QUEUED"}


# --- Backup Administration Routes ---

@router.post("/backup/export", response_model=ExportQueuedResponse)
async def trigger_export(
    background_tasks: BackgroundTasks,
    mode: str = Query("full", description="Backup mode: 'full' or 'delta'")
):
    config = BackupConfig.from_global_config()
    if not config.enabled:
        raise HTTPException(status_code=503, detail="Backup disabled")
    
    if mode not in ("full", "delta"):
        raise HTTPException(status_code=400, detail="mode must be 'full' or 'delta'")

    job_id = ULID.generate()
    created = now_iso()
    enqueue_write(
        "INSERT INTO jobs (job_id, session_id, tool_name, args_json, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, "0", "backup", json.dumps({"mode": mode}), "QUEUED", created, created)
    )
    _ensure_writer_running()

    background_tasks.add_task(BackupRunner.run, mode=mode, trigger_type="manual", manual_job_id=job_id)
    return ExportQueuedResponse(status="EXPORT_QUEUED", message=f"{mode.capitalize()} export started in background", job_id=job_id)


@router.get("/backup/status", response_model=BackupStatusResponse)
async def backup_status():
    status = BackupRunner.get_status()
    return BackupStatusResponse(
        enabled=status["enabled"],
        backup_dir=status["backup_dir"],
        watermark=status["watermark"],
        file_counts=status["file_counts"],
        total_size_bytes=status["total_size_bytes"]
    )


@router.post("/backup/restore", response_model=RestoreQueuedResponse)
async def trigger_restore(background_tasks: BackgroundTasks):
    config = BackupConfig.from_global_config()
    if not config.enabled:
        raise HTTPException(status_code=503, detail="Backup disabled")
    if browser_lock.locked():
        raise HTTPException(status_code=409, detail="System busy: active scraping job.")
        
    job_id = ULID.generate()
    created = now_iso()
    enqueue_write(
        "INSERT INTO jobs (job_id, session_id, tool_name, args_json, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, "0", "backup_restore", "{}", "QUEUED", created, created)
    )
    _ensure_writer_running()

    background_tasks.add_task(BackupRunner.restore, manual_job_id=job_id)
    return RestoreQueuedResponse(status="RESTORE_QUEUED", message="Restore started in background under browser_lock", job_id=job_id)


@router.get("/manifest")
async def manifest():
    REGISTRY.load_all()
    return {"tools": REGISTRY.schema_list()}


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str, request: Request):
    # Attempt to read canonical status from the DB; fall back to in-memory state.
    try:
        conn = DatabaseManager.get_read_connection()
        row = conn.execute("SELECT job_id, tool_name, status, args_json, created_at, updated_at FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
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

    # Attempt to read latest payload row from logs.db (payload_json) for final_payload
    final_payload = None
    try:
        conn = LogsDatabaseManager.get_read_connection()
        p = conn.execute("SELECT payload_json FROM logs WHERE job_id = ? AND payload_json IS NOT NULL ORDER BY timestamp DESC LIMIT 1", (job_id,)).fetchone()
        if p and p["payload_json"]:
            try:
                final_payload = json.loads(p["payload_json"])
            except Exception:
                final_payload = {"raw": p["payload_json"]}
            # If artifacts are present, add artifact_url entries
            if isinstance(final_payload, dict):
                arts = final_payload.get("artifacts") or final_payload.get("attachment_paths")
                if arts:
                    urls = []
                    for a in arts:
                        try:
                            # Normalize relative path and build absolute URL
                            url = artifact_url_from_request(request, a)
                            urls.append(url)
                        except Exception:
                            pass
                    final_payload["artifact_urls"] = urls
    except Exception:
        pass

    return JobStatusResponse(job_id=job_id, status=status_val, job_logs=job_logs, final_payload=final_payload)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_job(job_id: str, request: Request):
    """Request job cancellation. Marks job as CANCELLING and sets cancellation flag if running."""
    mgr = get_manager()
    flag = mgr.cancellation_flags.get(job_id) if mgr is not None else None

    # Persist cancellation request immediately
    ts = now_iso()
    try:
        enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ("CANCELLING", ts, job_id))
        # Write cancellation log to logs.db
        logs_enqueue_write(
            "INSERT INTO logs (id, job_id, tag, level, status_state, message, payload_json, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ULID.generate(), job_id, "system", "INFO", "CANCELLING", "Cancellation requested via API", None, ts),
        )
    except Exception:
        pass

    if flag:
        flag.set()
        return {"job_id": job_id, "status": "CANCELLING"}
    else:
        # Job not actively running yet; we marked it in DB and manager will honor it.
        return {"job_id": job_id, "status": "CANCELLING"}


@router.get("/metrics")
async def metrics():
    try:
        from database.writer import write_queue
        qsize = write_queue.qsize()
    except Exception:
        qsize = 0
    active_jobs = 0
    try:
        mgr = get_manager()
        active_jobs = len(mgr.cancellation_flags)
    except Exception:
        pass
    return {"write_queue_size": qsize, "active_jobs": active_jobs, "registered_tools": len(REGISTRY._tools)}
