# database/backup/runner.py
import json
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple
from pathlib import Path

from database.connection import DatabaseManager
from database.writer import enqueue_write
from database.backup.config import BackupConfig
from database.backup.models import ExportResult, RestoreResult
from database.backup.store_registry import StoreRegistry
from utils.browser_lock import browser_lock
from utils.logger import get_dual_logger
from utils.id_generator import ULID

log = get_dual_logger(__name__)

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

def read_watermark(config: BackupConfig) -> dict:
    if not config.watermark_path().exists():
        return {}
    try:
        with open(config.watermark_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def list_backup_files(config: BackupConfig) -> Tuple[int, Dict[str, int]]:
    counts = {}
    total_size = 0
    stores = StoreRegistry.get_all_stores()
    for name, store in stores.items():
        files = list(store.backup_dir.glob("*.json"))
        counts[name] = len(files)
        total_size += sum(f.stat().st_size for f in files)
        bin_files = list(store.backup_dir.glob("*.bin"))
        total_size += sum(f.stat().st_size for f in bin_files)
    return total_size, counts

def export_all_tables(conn, config: Optional[BackupConfig] = None, mode: str = "full") -> ExportResult:
    start = time.monotonic()
    total_counts = {}
    try:
        stores = StoreRegistry.get_all_stores()
        for name, store in stores.items():
            if hasattr(store, "export_from_sqlite"):
                res = store.export_from_sqlite(conn)
                total_counts[name] = res.get("exported", 0)
            if mode == "full" and hasattr(store, "cleanup_orphaned_files"):
                store.cleanup_orphaned_files(conn)
        return ExportResult(success=True, exported_counts=total_counts, duration_seconds=time.monotonic() - start)
    except Exception as e:
        log.dual_log(tag="Backup:Export:Error", message=f"Failed: {e}", level="ERROR", exc_info=e, payload={"error": str(e)})
        return ExportResult(success=False, error=str(e), duration_seconds=time.monotonic() - start)

def restore_master_tables_direct(conn) -> RestoreResult:
    start = time.monotonic()
    restored_counts = {}
    try:
        stores = StoreRegistry.get_all_stores()
        for name, store in stores.items():
            if hasattr(store, "reconcile"):
                summary = store.reconcile(conn)
                restored_counts[name] = summary.get("inserts", 0) + summary.get("updates", 0)
        return RestoreResult(success=True, restored_counts=restored_counts, duration_seconds=time.monotonic() - start)
    except Exception as e:
        log.dual_log(tag="Backup:Restore:Error", message=f"Restore failed: {e}", level="ERROR", exc_info=e, payload={"error": str(e)})
        return RestoreResult(success=False, error=str(e), duration_seconds=time.monotonic() - start)

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
            pass
        else:
            return ExportResult(success=False, error="Invalid trigger_type")

        start = time.monotonic()
        try:
            conn = DatabaseManager.get_read_connection()
            result = export_all_tables(conn, config, mode=mode)

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

        log.dual_log(tag="Backup:Restore:Lock", level="INFO", message="Waiting for browser_lock...", payload={"action": "wait_lock"})
        browser_lock.acquire()
        try:
            conn = DatabaseManager.get_read_connection()
            result = restore_master_tables_direct(conn)

            if manual_job_id:
                status = "COMPLETED" if result.success else "FAILED"
                enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", (status, _utcnow(), manual_job_id))

            return result
        except Exception as e:
            log.dual_log(tag="Backup:Restore:Error", message=f"Restore failed: {e}", level="ERROR", exc_info=e, payload={"error": str(e)})
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
            "watermark": wm,
            "file_counts": counts,
            "total_size_bytes": total_size,
        }
