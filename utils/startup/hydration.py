# utils/startup/hydration.py
import asyncio
from database.backup.config import BackupConfig
from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

async def hydrate_from_backup() -> None:
    config = BackupConfig.from_global_config()
    if not config.enabled or not config.backup_dir.exists():
        log.dual_log(tag="Startup:Hydration:Skip", message="Backup disabled or missing", level="INFO", payload={"action": "skip"})
        return
        
    log.dual_log(tag="Startup:Hydration:Started", message="Starting unified JSON backup hydration", level="INFO", payload={"backup_dir": str(config.backup_dir)})
    
    def _do_reconcile():
        from database.backup.store_registry import StoreRegistry
        from database.connection import DatabaseManager
        conn = DatabaseManager.get_read_connection()
        stores = StoreRegistry.get_all_stores()
        results = {}
        for name, store in stores.items():
            try:
                if hasattr(store, "reconcile"):
                    summary = store.reconcile(conn)
                    results[name] = summary
                    log.dual_log(tag="Startup:Hydration:Store", message=f"Reconciled {name}", level="INFO", payload={"table": name, **summary})
            except Exception as e:
                log.dual_log(tag="Startup:Hydration:StoreError", message=f"Failed to reconcile {name}: {e}", level="ERROR", payload={"table": name, "error": str(e)})
        return results

    try:
        result = await asyncio.to_thread(_do_reconcile)
        log.dual_log(tag="Startup:Hydration:Complete", message="Backup hydration completed successfully", level="INFO", payload={"status": "success", "results": result})
    except Exception as e:
        log.dual_log(tag="Startup:Hydration:Error", message=f"Hydration failed: {e}", level="CRITICAL", exc_info=e, payload={"error": str(e)})
        raise RuntimeError(f"Hydration failed: {e}") from e
