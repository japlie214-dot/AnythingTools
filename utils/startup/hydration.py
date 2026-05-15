# utils/startup/hydration.py
import asyncio
from database.backup.config import BackupConfig
from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

async def hydrate_from_backup() -> None:
    """Startup phase: hydrate database from Parquet backup files."""
    config = BackupConfig.from_global_config()
    if not config.enabled:
        log.dual_log(tag="Startup:Hydration:Disabled", message="Backup disabled, skipping hydration", level="INFO", payload={"action": "skip", "reason": "backup_disabled"})
        return
        
    if not config.backup_dir.exists():
        log.dual_log(tag="Startup:Hydration:NoBackup", message="No backup directory found, skipping hydration", level="INFO", payload={"action": "skip", "reason": "no_backup_dir", "path": str(config.backup_dir)})
        return
        
    log.dual_log(tag="Startup:Hydration:Started", message="Starting Parquet backup hydration", level="INFO", payload={"backup_dir": str(config.backup_dir)})
    
    try:
        from database.articles.bootstrap import reconcile_article_store
        await asyncio.to_thread(reconcile_article_store)
        log.dual_log(tag="Startup:Hydration:Articles", message="Article reconciliation complete", level="INFO", payload={"phase": "articles", "status": "success"})
    except Exception as e:
        log.dual_log(tag="Startup:Hydration:ArticleError", message=f"Article reconciliation failed: {e}", level="CRITICAL", exc_info=e, payload={"phase": "articles", "error": str(e)})
        raise RuntimeError(f"Article reconciliation failed: {e}") from e
        
    try:
        from database.backup.restore import restore_master_tables_direct
        
        def _do_restore():
            # Acquire connection INSIDE the background thread to satisfy SQLite thread-locality
            conn = DatabaseManager.get_read_connection()
            return restore_master_tables_direct(conn)
            
        result = await asyncio.to_thread(_do_restore)
        if result.success:
            log.dual_log(tag="Startup:Hydration:MasterTables", message="Master table hydration complete", level="INFO", payload={"phase": "master_tables", "status": "success", "restored_counts": result.restored_counts, "duration_s": result.duration_seconds})
        else:
            log.dual_log(tag="Startup:Hydration:MasterTableWarning", message=f"Master table hydration partial: {result.error}", level="WARNING", payload={"phase": "master_tables", "error": result.error})
    except Exception as e:
        log.dual_log(tag="Startup:Hydration:MasterTableError", message=f"Master table hydration failed: {e}", level="CRITICAL", exc_info=e, payload={"phase": "master_tables", "error": str(e)})
        raise RuntimeError(f"Master table hydration failed: {e}") from e

    log.dual_log(tag="Startup:Hydration:Complete", message="Backup hydration completed successfully", level="INFO", payload={"status": "success"})
