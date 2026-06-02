# utils/startup/hydration.py
import asyncio
from database.backup.settings import BackupSettings
from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

async def hydrate_from_backup() -> None:
    settings = BackupSettings()
    if not settings.local.enabled:
        log.dual_log(tag="Startup:Hydration:Skip", message="Backup disabled", level="INFO", payload={"action": "skip"})
        return
        
    log.dual_log(tag="Startup:Hydration:Started", message="Starting unified SQLite backup hydration", level="INFO", payload={"db_path": settings.local.db_path})
    
    def _do_reconcile():
        from utils.startup import _global_dual_engine
        if _global_dual_engine is None:
            log.dual_log(tag="Startup:Hydrate", message="Hydration skipped: DualEngine is offline")
            return {}
        result = _global_dual_engine.sync_all(mode="delta")
        return result

    try:
        result = await asyncio.to_thread(_do_reconcile)
        log.dual_log(tag="Startup:Hydration:Complete", message="Backup hydration completed successfully", level="INFO", payload={"status": "success", "results": result})
    except Exception as e:
        log.dual_log(tag="Startup:Hydration:Error", message=f"Hydration failed: {e}", level="CRITICAL", exc_info=e, payload={"error": str(e)})
        raise RuntimeError(f"Hydration failed: {e}") from e
