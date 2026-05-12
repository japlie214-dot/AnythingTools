# database/articles/bootstrap.py
import asyncio
from pathlib import Path
import pyarrow.parquet as pq
from database.backup.config import BackupConfig
from database.connection import DatabaseManager
from database.writer import enqueue_transaction, wait_for_writes
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def bootstrap_from_backup() -> None:
    """Populate database from Parquet backup files on startup."""
    config = BackupConfig.from_global_config()
    if not config.enabled or not config.backup_dir.exists():
        return
        
    conn = DatabaseManager.get_read_connection()
    try:
        existing_ids = {row[0] for row in conn.execute("SELECT id FROM scraped_articles").fetchall()}
    except Exception:
        existing_ids = set()
    
    for table in ["scraped_articles", "scraped_articles_vec"]:
        table_dir = config.backup_dir / table
        if not table_dir.exists():
            continue
        
        for pq_file in table_dir.glob("*.parquet"):
            if pq_file.name.endswith(".tmp.parquet"):
                continue
            try:
                pf = pq.ParquetFile(str(pq_file))
                for batch in pf.iter_batches(batch_size=500):
                    statements = []
                    for row in batch.to_pylist():
                        if table == "scraped_articles" and row.get("id") in existing_ids:
                            continue
                            
                        cols = list(row.keys())
                        placeholders = ", ".join("?" for _ in cols)
                        sql = f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
                        statements.append((sql, tuple(row.get(c) for c in cols)))
                        
                    if statements:
                        enqueue_transaction(statements)
            except Exception as e:
                log.dual_log(tag="Backup:Bootstrap:Error", level="ERROR", message=f"Failed reading {pq_file.name}", payload={"file": str(pq_file), "error": str(e)})
                
    try:
        loop = asyncio.get_running_loop()
        asyncio.run_coroutine_threadsafe(wait_for_writes(timeout=300.0), loop)
    except RuntimeError:
        asyncio.run(wait_for_writes(timeout=300.0))
    except Exception as e:
        log.dual_log(tag="Backup:Bootstrap:WaitError", level="WARNING", message=f"Wait for writes failed: {e}", payload={"error": str(e)})
