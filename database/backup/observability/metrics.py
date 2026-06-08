# database/backup/observability/metrics.py
from typing import Dict, Any
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class BackupMetricsCollector:
    @staticmethod
    def get_metrics(sync_engine) -> Dict[str, Any]:
        if not sync_engine:
            return {
                "cloud_engine": {"status": "offline"},
                "sync_status": {"pending_conflicts": 0, "dead_letter_count": 0},
                "circuit_breaker_state": "CLOSED"
            }
        try:
            cb_instance = getattr(sync_engine.cloud, "circuit_breaker", None)
            return {
                "cloud_engine": {"status": "ok" if sync_engine.cloud.settings.enabled else "disabled"},
                "sync_status": {"dead_letter_count": 0, "last_sync_time": None},
                "circuit_breaker_state": getattr(cb_instance, "state", "CLOSED") if cb_instance else "CLOSED"
            }
        except Exception:
            return {"cloud_engine": {"status": "unknown"}}
