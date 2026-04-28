# database/backup/runner.py
import json
import time
from datetime import datetime, timezone
from typing import Optional

from database.connection import DatabaseManager
from database.writer import enqueue_write
from database.backup.config import BackupConfig
from database.backup.models import ExportResult, RestoreResult
from database.backup.storage import export_all_tables, read_watermark, list_backup_files
from database.backup.restore import restore_master_tables_direct
from utils.browser_lock import browser_lock
from utils.logger import get_dual_logger
from utils.id_generator import ULID

log = get_dual_logger(__name__)

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

class BackupRunner:
    """Orchestrates backup operations with job tracking and concurrency safety."""

    @staticmethod
    def run(mode: str = "delta", trigger_type: str = "manual", parent_job_id: Optional[str] = None, manual_job_id: Optional[str] = None) -> ExportResult:
        config = BackupConfig.from_global_config()
        if not config.enabled:
            return ExportResult(success=False, error="Backup disabled")

        backup_job_id = None
        if trigger_type == "manual":
            backup_job_id = manual_job_id or ULID.generate()
            if manual_job_id:
                enqueue_write("UPDATE jobs SET status = 'RUNNING', updated_at = ? WHERE job_id = ?", (_utcnow(), backup_job_id))
            else:
                created = _utcnow()
                enqueue_write(
                    "INSERT INTO jobs (job_id, session_id, tool_name, args_json, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (backup_job_id, "0", "backup", json.dumps({"mode": mode, "trigger": "manual"}), "RUNNING", created, created)
                )
        elif trigger_type == "auto":
            # For auto triggers, the parent tool (e.g., scraper) manages its own job_items tracking.
            pass
        else:
            return ExportResult(success=False, error="Invalid trigger_type")

        start = time.monotonic()
        try:
            # Exports only require read access. Do not close the thread-local read connection.
            conn = DatabaseManager.get_read_connection()
            try:
                result = export_all_tables(conn, config, mode=mode)
            finally:
                pass

            if trigger_type == "manual" and backup_job_id:
                status = "COMPLETED" if result.success else "FAILED"
                enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", (status, _utcnow(), backup_job_id))

            return result
        except Exception as e:
            if trigger_type == "manual" and backup_job_id:
                enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ("FAILED", _utcnow(), backup_job_id))
            return ExportResult(success=False, error=str(e), duration_seconds=time.monotonic() - start)

    @staticmethod
    def restore(manual_job_id: Optional[str] = None) -> RestoreResult:
        config = BackupConfig.from_global_config()
        if not config.enabled:
            return RestoreResult(success=False, error="Backup disabled")

        if manual_job_id:
            enqueue_write("UPDATE jobs SET status = 'RUNNING', updated_at = ? WHERE job_id = ?", (_utcnow(), manual_job_id))

        start = time.monotonic()

        # Block in background queue until scraper finishes
        log.dual_log(tag="Backup:Restore", level="INFO", message="Waiting for browser_lock...")
        browser_lock.acquire()
        try:
            # Restore only requires read access to schema info; writes are routed via enqueue_transaction.
            conn = DatabaseManager.get_read_connection()
            try:
                result = restore_master_tables_direct(conn)
            finally:
                pass

            if manual_job_id:
                status = "COMPLETED" if result.success else "FAILED"
                enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", (status, _utcnow(), manual_job_id))

            return result
        except Exception as e:
            log.dual_log(tag="Backup:Restore:Error", message=f"Restore failed: {e}", level="ERROR", exc_info=e)
            if manual_job_id:
                enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ("FAILED", _utcnow(), manual_job_id))
            return RestoreResult(success=False, error=str(e), duration_seconds=time.monotonic() - start)
        finally:
            browser_lock.safe_release()

    @staticmethod
    def get_status() -> dict:
        config = BackupConfig.from_global_config()
        wm = read_watermark(config)
        total_size, counts = list_backup_files(config)
        return {
            "enabled": config.enabled,
            "backup_dir": str(config.backup_dir),
            "watermark": wm.model_dump_compat(),
            "file_counts": counts,
            "total_size_bytes": total_size,
        }