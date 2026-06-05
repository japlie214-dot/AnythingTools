# database/backup/observability/metrics.py
from typing import Dict, Any
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class BackupMetricsCollector:
    @staticmethod
    def get_metrics(dual_engine) -> Dict[str, Any]:
        if not dual_engine:
            return {
                "local_engine": {"status": "offline"},
                "cloud_engine": {"status": "offline"},
                "sync_status": {"pending_conflicts": 0, "dead_letter_count": 0, "last_sync_time": None},
                "circuit_breaker_state": "CLOSED"
            }
        try:
            import sqlite3
            local_db = dual_engine.local.db_path
            pending_count = 0
            dlq_count = 0
            last_sync = None
            try:
                conn = sqlite3.connect(local_db, timeout=5.0)
                cursor = conn.execute("SELECT count(*) FROM sync_ledger WHERE state = 'PENDING'")
                pending_count = cursor.fetchone()[0]
                cursor = conn.execute("SELECT count(*) FROM dead_letter_queue")
                dlq_count = cursor.fetchone()[0]
                cursor = conn.execute("SELECT max(completed_at) FROM sync_ledger WHERE state = 'COMPLETED'")
                last_sync = cursor.fetchone()[0]
                conn.close()
            except Exception:
                pass
            cb_instance = getattr(dual_engine.cloud, "circuit_breaker", None)
            return {
                "local_engine": {"status": "ok" if dual_engine.local.settings.enabled else "disabled"},
                "cloud_engine": {"status": "ok" if dual_engine.cloud.settings.enabled else "disabled"},
                "sync_status": {"pending_conflicts": pending_count, "dead_letter_count": dlq_count, "last_sync_time": last_sync},
                "circuit_breaker_state": getattr(cb_instance, "state", "CLOSED") if cb_instance else "CLOSED"
            }
        except Exception:
            return {
                "local_engine": {"status": "unknown"},
                "cloud_engine": {"status": "unknown"},
                "sync_status": {"pending_conflicts": 0, "dead_letter_count": 0, "last_sync_time": None},
                "circuit_breaker_state": "UNKNOWN"
            }
