# api/routes.py
from fastapi import APIRouter, HTTPException, status, Request, Depends
from typing import Dict, Any
import threading
import asyncio
import importlib
import json
from datetime import datetime, timezone

import config
from api.schemas import JobCreateRequest, JobCreateResponse, JobStatusResponse, JobLogEntry
from tools.registry import REGISTRY
from utils.logger.core import get_dual_logger
from utils.id_generator import ULID
from database.writer import start_writer, enqueue_write
from database.connection import DatabaseManager
from utils.artifact_manager import artifact_url_from_request
from bot.engine.worker import get_manager
from utils.security import scan_args_for_urls
from pydantic import ValidationError

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
        raise HTTPException(status_code=404, detail="Tool not found")

    # Input validation using optional INPUT_MODEL exported by the tool module
    InputModel = None
    try:
        module = importlib.import_module(meta.get("module"))
        InputModel = getattr(module, "INPUT_MODEL", None)
    except Exception:
        InputModel = None

    try:
        if InputModel is not None:
            validated_args = InputModel.parse_obj(req.args)
            args = validated_args.dict()
        else:
            args = req.args
        
        if req.client_metadata:
            args["_client_metadata"] = req.client_metadata
    except ValidationError as e:
        # Return 422 with structured validation errors
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.errors())
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

    # Load job logs from persistent store (job_logs)
    job_logs = []
    try:
        conn = DatabaseManager.get_read_connection()
        rows = conn.execute(
            "SELECT timestamp, level, tag, status_state, message FROM job_logs WHERE job_id = ? ORDER BY timestamp",
            (job_id,),
        ).fetchall()
        for r in rows:
            job_logs.append(JobLogEntry(timestamp=r["timestamp"], level=r["level"], tag=r["tag"], status_state=r["status_state"], message=r["message"]))
    except Exception:
        pass

    # Attempt to read latest payload row from job_logs (payload_json) for final_payload
    final_payload = None
    try:
        conn = DatabaseManager.get_read_connection()
        p = conn.execute("SELECT payload_json FROM job_logs WHERE job_id = ? AND payload_json IS NOT NULL ORDER BY timestamp DESC LIMIT 1", (job_id,)).fetchone()
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
        enqueue_write(
            "INSERT INTO job_logs (id, job_id, tag, level, status_state, message, payload_json, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
