# tools/backup/restore.py
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
import pyarrow.parquet as pq
import sqlite3
from tools.backup.config import BackupConfig
from tools.backup.models import RestoreResult
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def _get_latest_parquet_file(table_dir: Path, table_name: str) -> Optional[Path]:
    """Get the path to the most recent Parquet snapshot for a table."""
    files = sorted(table_dir.glob(f"{table_name}_*.parquet"))
    return files[-1] if files else None

def _get_columns(conn: sqlite3.Connection, table_name: str) -> List[Dict[str, Any]]:
    """Get column metadata for a table."""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    columns = []
    for row in cursor.fetchall():
        columns.append({
            "cid": row[0],
            "name": row[1],
            "type": row[2],
            "notnull": row[3],
            "dflt_value": row[4],
            "pk": row[5]
        })
    return columns

def _build_insert_sql(table_name: str, desired_cols: List[Dict[str, Any]], file_cols: List[str]) -> Optional[tuple[str, List[str]]]:
    """
    Build an adaptive INSERT statement that matches columns by name.
    - Skips columns not in the backup
    - Allows SQLite to apply DEFAULT values for missing columns
    - Validates that required PK and NOT NULL columns exist
    """
    # Check Primary Keys exist
    pk_cols = [c["name"] for c in desired_cols if c["pk"]]
    missing_pk = [c for c in pk_cols if c not in file_cols]
    if missing_pk:
        log.dual_log(tag="Backup:Restore", level="WARNING", message=f"Skipping {table_name}: missing PK columns {missing_pk}")
        return None

    # Check required NOT NULL columns (not PK, no default)
    required = [c["name"] for c in desired_cols if c["notnull"] and not c["dflt_value"] and not c["pk"]]
    missing_required = [c for c in required if c not in file_cols]
    if missing_required:
        log.dual_log(tag="Backup:Restore", level="WARNING", message=f"Skipping {table_name}: missing required NOT NULL columns {missing_required}")
        return None

    # Build column list - only include columns that exist in backup
    matched_cols = []
    placeholders = []
    for c in desired_cols:
        if c["name"] in file_cols:
            matched_cols.append(c["name"])
            placeholders.append("?")

    sql = f"INSERT OR IGNORE INTO {table_name} ({', '.join(matched_cols)}) VALUES ({', '.join(placeholders)})"
    return sql, matched_cols

def restore_master_tables_direct(conn: sqlite3.Connection, table_names: Optional[List[str]] = None) -> RestoreResult:
    """
    Intelligent restoration for master tables.
    Uses PyArrow streaming to avoid OOM, and dispatches writes through the single-writer queue.
    """
    start = time.monotonic()
    config = BackupConfig.from_global_config()
    if not config.enabled:
        return RestoreResult(success=False, error="Backup disabled")

    from database.schemas import MASTER_TABLES
    if table_names is None:
        table_names = list(MASTER_TABLES)

    restored_counts: Dict[str, int] = {}

    for table_name in table_names:
        if table_name not in MASTER_TABLES:
            continue

        latest_file = _get_latest_parquet_file(config.table_dir(table_name), table_name)
        if not latest_file:
            log.dual_log(tag="Backup:Restore", level="INFO", message=f"No backup data for {table_name}")
            continue

        desired_cols = _get_columns(conn, table_name)
        if not desired_cols:
            log.dual_log(tag="Backup:Restore", level="WARNING", message=f"Table {table_name} does not exist; cannot restore")
            continue

        try:
            parquet_file = pq.ParquetFile(latest_file)
            file_cols = parquet_file.schema.names
        except Exception as e:
            log.dual_log(tag="Backup:Restore", level="ERROR", message=f"Failed to read Parquet file for {table_name}: {e}")
            continue

        build_result = _build_insert_sql(table_name, desired_cols, file_cols)
        if build_result is None:
            continue
        sql, matched_cols = build_result

        count = 0
        # Route all writes through the single-writer queue in transaction-sized batches
        from database.writer import enqueue_transaction, wait_for_writes
        import asyncio

        try:
            for batch in parquet_file.iter_batches(batch_size=500):
                pylist = batch.to_pylist()
                statements: list[tuple[str, tuple]] = []
                for row in pylist:
                    params = []
                    for col_name in matched_cols:
                        params.append(row.get(col_name))
                    statements.append((sql, tuple(params)))

                if statements:
                    enqueue_transaction(statements)
                    count += len(statements)

            # Synchronously wait for the background writer to commit transactions for this table
            try:
                loop = asyncio.get_running_loop()
                asyncio.run_coroutine_threadsafe(wait_for_writes(timeout=120.0), loop).result()
            except RuntimeError:
                # No running loop in this thread; run synchronously
                asyncio.run(wait_for_writes(timeout=120.0))

        except Exception as e:
            log.dual_log(tag="Backup:Restore", level="ERROR", message=f"Transaction failed during restore of {table_name}: {e}")
            continue

        restored_counts[table_name] = count
        log.dual_log(tag="Backup:Restore", level="INFO", message=f"Restored {count} rows into {table_name}")

    # Synchronous FTS rebuild at the tail-end
    if restored_counts.get("scraped_articles", 0) > 0:
        log.dual_log(tag="Backup:Restore", level="INFO", message="Rebuilding FTS index synchronously...")
        from database.writer import enqueue_write, wait_for_writes
        import asyncio
        enqueue_write("INSERT INTO scraped_articles_fts(scraped_articles_fts) VALUES('rebuild')")
        try:
            try:
                loop = asyncio.get_running_loop()
                asyncio.run_coroutine_threadsafe(wait_for_writes(timeout=300.0), loop).result()
            except RuntimeError:
                asyncio.run(wait_for_writes(timeout=300.0))
            log.dual_log(tag="Backup:Restore", level="INFO", message="FTS index rebuilt successfully.")
        except Exception as e:
            log.dual_log(tag="Backup:Restore", level="ERROR", message=f"FTS rebuild failed: {e}")

    return RestoreResult(success=True, restored_counts=restored_counts, duration_seconds=time.monotonic() - start)
