# database/backup/observability/metrics.py
from typing import Dict, Any
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class BackupMetricsCollector:
    _cloud_writer_stats = {"flush_success": 0, "flush_error": 0, "retry_count": 0, "dlq_count": 0}

    @staticmethod
    def get_metrics(sync_engine) -> Dict[str, Any]:
        if not sync_engine:
            return {
                "cloud_engine": {"status": "offline"},
                "sync_status": {"pending_conflicts": 0, "dead_letter_count": 0, "cloud_writer_stats": BackupMetricsCollector._cloud_writer_stats},
                "circuit_breaker_state": "CLOSED"
            }
        try:
            cb_instance = getattr(sync_engine.cloud, "circuit_breaker", None)
            return {
                "cloud_engine": {"status": "ok" if sync_engine.cloud.settings.enabled else "disabled"},
                "sync_status": {"dead_letter_count": 0, "last_sync_time": None, "cloud_writer_stats": BackupMetricsCollector._cloud_writer_stats},
                "circuit_breaker_state": getattr(cb_instance, "state", "CLOSED") if cb_instance else "CLOSED"
            }
        except Exception:
            return {"cloud_engine": {"status": "unknown"}}

    @staticmethod
    def record_flush(success: bool, retried: bool = False, dlq: bool = False):
        if success:
            BackupMetricsCollector._cloud_writer_stats["flush_success"] += 1
        else:
            BackupMetricsCollector._cloud_writer_stats["flush_error"] += 1
        if retried:
            BackupMetricsCollector._cloud_writer_stats["retry_count"] += 1
        if dlq:
            BackupMetricsCollector._cloud_writer_stats["dlq_count"] += 1
