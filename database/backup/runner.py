import time
from typing import Optional
from database.backup.models import ExportResult, RestoreResult
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def _get_sync_engine():
    from utils.startup import _global_sync_engine
    if _global_sync_engine is None:
        from database.backup.settings import BackupSettings
        from database.backup.engine.sync_engine import SyncEngine
        engine = SyncEngine(BackupSettings())
        engine.startup()
        return engine
    return _global_sync_engine

class BackupRunner:
    @staticmethod
    def run(mode: str = "delta", trigger_type: str = "manual", parent_job_id: Optional[str] = None, manual_job_id: Optional[str] = None) -> ExportResult:
        engine = _get_sync_engine()
        result = engine.sync_all(mode=mode)
        return ExportResult(
            success=result.get("cloud_error") is None,
            exported_counts=result.get("cloud", {}),
            duration_seconds=result.get("duration", 0.0),
            error=result.get("cloud_error")
        )

    @staticmethod
    def restore(manual_job_id: Optional[str] = None) -> RestoreResult:
        engine = _get_sync_engine()
        start = time.monotonic()
        try:
            success = engine.restore_pipeline()
            return RestoreResult(success=success, duration_seconds=time.monotonic() - start)
        except Exception as e:
            return RestoreResult(success=False, error=str(e), duration_seconds=time.monotonic() - start)
