# database/sse_retire_pending_callback.py
"""One-shot retirement of legacy PENDING_CALLBACK rows.

do NOT route through DualDBMigrationCoordinator — that's for
DDL type-mismatch table cloning, not data updates. This is a simple
enqueue_write sequence invoked from the startup routine.

Idempotent: re-running on a clean DB is a no-op (no PENDING_CALLBACK rows).
"""
import json
from datetime import datetime, timezone
from typing import Optional

from database.connection import DatabaseManager
from database.writer import enqueue_write
from utils.logger.core import get_dual_logger
from utils.id_generator import ULID

log = get_dual_logger(__name__)


def retire_pending_callback_jobs() -> int:
    """Convert all PENDING_CALLBACK rows to FAILED with a retired_at marker.

    Returns the count of retired rows. Safe to call on every startup —
    returns 0 if no PENDING_CALLBACK rows exist.
    """
    try:
        conn = DatabaseManager.get_read_connection()
        rows = conn.execute(
            "SELECT job_id, result_json FROM jobs WHERE status = 'PENDING_CALLBACK'"
        ).fetchall()
    except Exception as e:
        log.dual_log(
            tag="SSE:RetirePcb:ReadError",
            message=f"Failed to read PENDING_CALLBACK rows: {e}",
            level="WARNING",
            payload={"error": str(e)},
        )
        return 0

    if not rows:
        return 0

    retired_at = datetime.now(timezone.utc).isoformat()
    count = 0
    for row in rows:
        job_id = row["job_id"]
        # Merge retired_at into the existing result_json without losing data.
        existing = {}
        try:
            if row["result_json"]:
                existing = json.loads(row["result_json"])
        except Exception:
            existing = {"_raw": row["result_json"]}
        if isinstance(existing, dict):
            existing["_retired_at"] = retired_at
            existing["_retire_reason"] = "PENDING_CALLBACK removed in SSE refactor"
        else:
            existing = {"_retired_at": retired_at, "_retire_reason": "PENDING_CALLBACK removed", "_raw": existing}
        new_json = json.dumps(existing, ensure_ascii=False, default=str)

        enqueue_write(
            "UPDATE jobs SET status = 'FAILED', result_json = ?, updated_at = ? WHERE job_id = ?",
            (new_json, retired_at, job_id),
        )
        # Log the retirement so the audit trail is in logs.db.
        from database.logs_writer import logs_enqueue_write
        logs_enqueue_write(
            "INSERT INTO logs (id, job_id, tag, level, status_state, message, payload_json, event_id, error_json, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ULID.generate(), job_id, "SSE:RetirePcb:Row", "INFO", "FAILED",
             "Retired PENDING_CALLBACK job to FAILED", json.dumps({"retired_at": retired_at}),
             ULID.generate(), None, retired_at),
        )
        count += 1

    log.dual_log(
        tag="SSE:RetirePcb:Complete",
        message=f"Retired {count} PENDING_CALLBACK job(s) to FAILED",
        level="INFO",
        payload={"retired_count": count, "retired_at": retired_at},
    )
    return count
