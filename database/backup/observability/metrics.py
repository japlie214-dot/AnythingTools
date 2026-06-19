# database/backup/observability/metrics.py
"""Backup metrics collector for the /api/backup/status endpoint.

Previously, this module returned hardcoded values for dead_letter_count
and last_sync_time, which made the /api/backup/status endpoint misleading.
This revision queries the real values from the operational SQLite database:
  - dead_letter_count: SELECT COUNT(*) FROM dead_letter_queue
  - last_sync_time: SELECT max(completed_at) FROM sync_ledger WHERE state='COMPLETED'

Per the SQLite documentation, opening a read-only connection with a short
busy_timeout is safe even while the writer thread is active (WAL mode
allows concurrent readers):
https://www.sqlite.org/wal.html
"""
import sqlite3
from typing import Dict, Any
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


class BackupMetricsCollector:
    """Collects and reports backup/cloud-sync metrics.

    The _cloud_writer_stats dict is a module-level mutable counter updated
    by record_flush() from the cloud-writer thread. It is NOT thread-safe
    (Python's GIL provides sufficient protection for int increments).
    """
    _cloud_writer_stats = {"flush_success": 0, "flush_error": 0, "retry_count": 0, "dlq_count": 0}

    @staticmethod
    def _query_dead_letter_count() -> int:
        """Query the actual dead_letter_queue row count from the operational DB.

        Returns -1 if the table cannot be queried (e.g. DB not initialized,
        table missing, connection timeout). The -1 sentinel allows the API
        consumer to distinguish "0 dead letters" from "unknown".
        """
        try:
            from database.connection import DB_PATH
            # Per SQLite WAL docs: https://www.sqlite.org/wal.html
            # "Readers do not block writers and writers do not block readers."
            conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
            try:
                row = conn.execute("SELECT COUNT(*) FROM dead_letter_queue").fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception as e:
            log.dual_log(
                tag="Backup:Metrics:QueryFailed",
                level="DEBUG",
                message=f"Could not query dead_letter_queue count: {e}",
                payload={"error": str(e)[:200]},
            )
            return -1

    @staticmethod
    def _query_last_sync_time() -> str | None:
        """Query the last successful sync completion time from sync_ledger.

        Returns None if no sync has completed yet, or if the table cannot
        be queried.
        """
        try:
            from database.connection import DB_PATH
            conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
            try:
                row = conn.execute(
                    "SELECT max(completed_at) FROM sync_ledger WHERE state = 'COMPLETED'"
                ).fetchone()
                return row[0] if row and row[0] else None
            finally:
                conn.close()
        except Exception as e:
            log.dual_log(
                tag="Backup:Metrics:QueryFailed",
                level="DEBUG",
                message=f"Could not query sync_ledger last_sync_time: {e}",
                payload={"error": str(e)[:200]},
            )
            return None

    @staticmethod
    def get_metrics(sync_engine) -> Dict[str, Any]:
        """Return a metrics dict for the /api/backup/status endpoint.

        Args:
            sync_engine: The global SyncEngine instance, or None if backup
                is not initialized.

        Returns:
            Dict with keys: cloud_engine.status, sync_status.dead_letter_count,
            sync_status.last_sync_time, sync_status.cloud_writer_stats,
            circuit_breaker_state.
        """
        dlq_count = BackupMetricsCollector._query_dead_letter_count()
        last_sync = BackupMetricsCollector._query_last_sync_time()

        if not sync_engine:
            return {
                "cloud_engine": {"status": "offline"},
                "sync_status": {
                    "pending_conflicts": 0,
                    "dead_letter_count": dlq_count,
                    "last_sync_time": last_sync,
                    "cloud_writer_stats": BackupMetricsCollector._cloud_writer_stats,
                },
                "circuit_breaker_state": "CLOSED",
            }
        try:
            cb_instance = getattr(sync_engine.cloud, "circuit_breaker", None)
            return {
                "cloud_engine": {
                    "status": "ok" if sync_engine.cloud.settings.enabled else "disabled"
                },
                "sync_status": {
                    "dead_letter_count": dlq_count,
                    "last_sync_time": last_sync,
                    "cloud_writer_stats": BackupMetricsCollector._cloud_writer_stats,
                },
                "circuit_breaker_state": getattr(cb_instance, "state", "CLOSED") if cb_instance else "CLOSED",
            }
        except Exception as e:
            log.dual_log(
                tag="Backup:Metrics:QueryFailed",
                level="WARNING",
                message=f"Could not collect full metrics: {e}",
                payload={"error": str(e)[:200]},
            )
            return {"cloud_engine": {"status": "unknown"}}

    @staticmethod
    def record_flush(success: bool, retried: bool = False, dlq: bool = False):
        """Record a cloud-writer flush outcome. Called from _flush_batch."""
        if success:
            BackupMetricsCollector._cloud_writer_stats["flush_success"] += 1
        else:
            BackupMetricsCollector._cloud_writer_stats["flush_error"] += 1
        if retried:
            BackupMetricsCollector._cloud_writer_stats["retry_count"] += 1
        if dlq:
            BackupMetricsCollector._cloud_writer_stats["dlq_count"] += 1
