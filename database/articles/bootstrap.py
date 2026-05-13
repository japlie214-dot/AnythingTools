# database/articles/bootstrap.py
import asyncio
import time
from pathlib import Path
from database.backup.config import BackupConfig
from database.connection import DatabaseManager
from database.writer import enqueue_transaction, wait_for_writes
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def reconcile_article_store() -> None:
    """Run delta reconciliation between ArticleStore manifest and SQLite on startup."""
    from database.articles.store import get_article_store
    from database.articles.reconcile import reconcile_delta

    config = BackupConfig.from_global_config()
    if not config.enabled or not config.backup_dir.exists():
        return

    try:
        store = get_article_store()
        summary = reconcile_delta(store)
        if summary.get("deletes") or summary.get("inserts") or summary.get("updates"):
            log.dual_log(
                tag="Backup:Reconcile:Applied",
                level="INFO",
                message="Article store reconciliation applied changes",
                payload=summary,
            )
    except Exception as e:
        log.dual_log(
            tag="Backup:Reconcile:Error",
            level="ERROR",
            message=f"Article store reconciliation failed: {e}",
            payload={"error": str(e)},
        )
