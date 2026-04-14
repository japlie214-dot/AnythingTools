# database/job_queue.py
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from database.connection import DatabaseManager
from database.writer import enqueue_write
from utils.id_generator import ULID


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(session_id: str, tool_name: str, args_json: str) -> str:
    """Create a job record following the mandatory 3-step insertion order (Golden Rule 6).
    Returns the new job_id.
    """
    job_id = ULID.generate()
    # Step 1: ensure session row exists (active_job_id stays NULL for now)
    enqueue_write(
        "INSERT INTO sessions (session_id) VALUES (?) ON CONFLICT(session_id) DO NOTHING",
        (session_id,)
    )
    # Step 2: create the job row
    enqueue_write(
        "INSERT INTO jobs (job_id, session_id, tool_name, args_json, status, updated_at) "
        "VALUES (?, ?, ?, ?, 'RUNNING', ?)",
        (job_id, session_id, tool_name, args_json, _utcnow())
    )
    # Step 3: link the job to the session
    enqueue_write(
        "UPDATE sessions SET active_job_id = ?, is_busy = 1 WHERE session_id = ?",
        (job_id, session_id)
    )
    return job_id


def add_job_item(job_id: str, step_identifier: str, input_data: str) -> None:
    """Insert a job_item row via the async writer queue.
    Idempotent via conditional insertion. Does not return an item_id to avoid blocking.
    """
    enqueue_write(
        "INSERT INTO job_items (job_id, step_identifier, input_data, updated_at) "
        "SELECT ?, ?, ?, ? "
        "WHERE NOT EXISTS (SELECT 1 FROM job_items WHERE job_id = ? AND step_identifier = ?)",
        (job_id, step_identifier, input_data, _utcnow(), job_id, step_identifier)
    )


def update_item_status(job_id: str, step_identifier: str, status: str, output_data: str) -> None:
    enqueue_write(
        "UPDATE job_items SET status = ?, output_data = ?, updated_at = ? WHERE job_id = ? AND step_identifier = ?",
        (status, output_data, _utcnow(), job_id, step_identifier)
    )


def update_job_heartbeat(job_id: str) -> None:
    enqueue_write(
        "UPDATE jobs SET updated_at = ? WHERE job_id = ?",
        (_utcnow(), job_id)
    )


def mark_job_interrupted(job_id: str) -> None:
    enqueue_write(
        "UPDATE jobs SET status = 'INTERRUPTED', updated_at = ? WHERE job_id = ?",
        (_utcnow(), job_id)
   )


def get_interrupted_job(session_id: str, tool_name: str) -> Optional[Dict[str, Any]]:
    """Return the most recently interrupted job for this session + tool, or None.
    Only 'INTERRUPTED' status is resumable; 'PENDING' (never started) is excluded.
    """
    from database.reader import execute_read_sql
    rows = execute_read_sql(
        "SELECT job_id, session_id, tool_name, args_json, status, retry_count, updated_at "
        "FROM jobs WHERE session_id = ? AND tool_name = ? AND status = 'INTERRUPTED' "
        "ORDER BY updated_at DESC LIMIT 1",
        (session_id, tool_name)
    )
    return rows[0] if rows else None
