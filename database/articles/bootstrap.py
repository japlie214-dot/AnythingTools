# database/articles/bootstrap.py
import asyncio
import time
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
                        from database.writer import write_queue
                        while write_queue.maxsize > 0 and write_queue.qsize() >= write_queue.maxsize - 5:
                            time.sleep(0.05)
                        enqueue_transaction(statements)
            except Exception as e:
                log.dual_log(tag="Backup:Bootstrap:Error", level="ERROR", message=f"Failed reading {pq_file.name}", payload={"file": str(pq_file), "error": str(e)})
                
    try:
        from database.writer import write_queue
        _max_wait = 300.0
        _start = time.monotonic()
        
        while not write_queue.empty() and (time.monotonic() - _start < _max_wait):
            time.sleep(0.5)
            
        if not write_queue.empty():
            log.dual_log(
                tag="Backup:Bootstrap:Timeout",
                level="WARNING",
                message=f"Write queue not empty after {_max_wait}s",
                payload={"remaining": write_queue.qsize()}
            )
    except Exception as e:
        log.dual_log(tag="Backup:Bootstrap:WaitError", level="WARNING", message=f"Wait for writes failed: {e}", payload={"error": str(e)})
