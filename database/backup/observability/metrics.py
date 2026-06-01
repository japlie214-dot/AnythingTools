# database/backup/observability/metrics.py
from typing import Dict, Any
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class BackupMetricsCollector:
    @staticmethod
    def get_metrics(dual_engine) -> Dict[str, Any]:
        """Gathers health and sync metrics from the DualEngine."""
        if not dual_engine:
            return {
                "local_engine": {"status": "offline"},
                "cloud_engine": {"status": "offline"},
                "sync_status": {"pending_conflicts": 0, "dead_letter_count": 0, "last_sync_time": None},
                "circuit_breaker_state": "CLOSED"
            }
            
        return {
            "local_engine": {"status": "ok" if dual_engine.local.settings.enabled else "disabled"},
            "cloud_engine": {"status": "ok" if dual_engine.cloud.settings.enabled else "disabled"},
            "sync_status": {
                "pending_conflicts": 0,  # Query from sync_ledger where state = 'PENDING'
                "dead_letter_count": 0,  # Query from dead_letter_queue count
                "last_sync_time": None
            },
            "circuit_breaker_state": getattr(dual_engine.cloud.circuit_breaker, 'state', 'CLOSED')
        }
