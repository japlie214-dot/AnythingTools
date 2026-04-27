# utils/startup/recovery.py

import sqlite3
from database.connection import DatabaseManager
from database.writer import enqueue_write
from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)

async def run_startup_recovery() -> None:
    """Startup healing pass: mark stale RUNNING jobs as INTERRUPTED and purge old inactive jobs."""
    try:
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        
        # Identify jobs stuck in RUNNING status from prior unclean shutdowns
        for row in conn.execute("SELECT job_id FROM jobs WHERE status = 'RUNNING'").fetchall():
            enqueue_write(
                "UPDATE jobs SET status = 'INTERRUPTED', updated_at = datetime('now') WHERE job_id = ?",
                (row['job_id'],)
            )
        log.dual_log(tag="Startup:Recovery", message="Startup recovery scan complete.")
        
        # Purge stale or abandoned job metadata older than 7 days
        for row in conn.execute(
            "SELECT job_id FROM jobs WHERE status IN ('RUNNING','PENDING','INTERRUPTED') "
            "AND updated_at < datetime('now', '-7 days')"
        ).fetchall():
            enqueue_write("UPDATE jobs SET status = 'FAILED' WHERE job_id = ?", (row['job_id'],))
            enqueue_write("DELETE FROM job_items WHERE job_id = ?", (row['job_id'],))
            
        log.dual_log(tag="Startup:Cleanup", message="Stale job cleanup complete.")
    except Exception as e:
        log.dual_log(tag="Startup:Recovery", message=f"Recovery scan error: {e}", level="ERROR")
