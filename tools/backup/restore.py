# tools/backup/restore.py
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
import pandas as pd
import sqlite3
from database.writer import enqueue_write, enqueue_execscript, wait_for_writes
from tools.backup.config import BackupConfig
from tools.backup.models import RestoreResult
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def _read_latest_parquet(table_dir: Path, table_name: str) -> pd.DataFrame:
    """Read the most recent Parquet snapshot for a table."""
    files = sorted(table_dir.glob(f"{table_name}_*.parquet"))
    if not files: return pd.DataFrame()
    return pd.read_parquet(files[-1], engine="pyarrow")

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

def _build_insert_sql(table_name: str, desired_cols: List[Dict[str, Any]], df: pd.DataFrame) -> Optional[tuple[str, List[str]]]:
    """
    Build an adaptive INSERT statement that matches columns by name.
    - Skips columns not in the backup
    - Allows SQLite to apply DEFAULT values for missing columns
    - Validates that required PK and NOT NULL columns exist
    """
    # Check Primary Keys exist
    pk_cols = [c["name"] for c in desired_cols if c["pk"]]
    missing_pk = [c for c in pk_cols if c not in df.columns]
    if missing_pk:
        log.dual_log(tag="Backup:Restore", level="WARNING", message=f"Skipping {table_name}: missing PK columns {missing_pk}")
        return None

    # Check required NOT NULL columns (not PK, no default)
    required = [c["name"] for c in desired_cols if c["notnull"] and not c["dflt_value"] and not c["pk"]]
    missing_required = [c for c in required if c not in df.columns]
    if missing_required:
        log.dual_log(tag="Backup:Restore", level="WARNING", message=f"Skipping {table_name}: missing required NOT NULL columns {missing_required}")
        return None

    # Build column list - only include columns that exist in backup
    matched_cols = []
    placeholders = []
    for c in desired_cols:
        if c["name"] in df.columns:
            matched_cols.append(c["name"])
            placeholders.append("?")

    sql = f"INSERT INTO {table_name} ({', '.join(matched_cols)}) VALUES ({', '.join(placeholders)})"
    return sql, matched_cols

def restore_master_tables_direct(conn: sqlite3.Connection, table_names: Optional[List[str]] = None) -> RestoreResult:
    """
    Intelligent restoration for master tables.
    Uses column-by-column mapping and allows SQLite defaults for missing columns.
    """
    start = time.monotonic()
    config = BackupConfig.from_global_config()
    if not config.enabled:
        return RestoreResult(success=False, error="Backup disabled")

    from database.schemas import MASTER_TABLES
    if table_names is None: table_names = list(MASTER_TABLES)
    
    restored_counts: Dict[str, int] = {}

    for table_name in table_names:
        if table_name not in MASTER_TABLES: continue

        df = _read_latest_parquet(config.table_dir(table_name), table_name)
        if df.empty:
            log.dual_log(tag="Backup:Restore", level="INFO", message=f"No backup data for {table_name}")
            continue

        desired_cols = _get_columns(conn, table_name)
        if not desired_cols:
            log.dual_log(tag="Backup:Restore", level="WARNING", message=f"Table {table_name} does not exist; cannot restore")
            continue

        build_result = _build_insert_sql(table_name, desired_cols, df)
        if build_result is None: continue
        sql, matched_cols = build_result

        count = 0
        for _, row in df.iterrows():
            params: List[Any] = []
            for col_name in matched_cols:
                val = row[col_name]
                # Guard pd.isna against raw byte arrays to prevent TypeErrors
                if not isinstance(val, (bytes, memoryview, bytearray)) and pd.isna(val): 
                    val = None
                # Handle byte arrays (vector embeddings)
                if isinstance(val, memoryview): val = bytes(val)
                params.append(val)
            try:
                conn.execute(sql, params)
                count += 1
            except sqlite3.IntegrityError as e:
                log.dual_log(tag="Backup:Restore", level="WARNING", message=f"Integrity error restoring {table_name}: {e}")
                continue

        restored_counts[table_name] = count
        log.dual_log(tag="Backup:Restore", level="INFO", message=f"Restored {count} rows into {table_name}")

    return RestoreResult(success=True, restored_counts=restored_counts, duration_seconds=time.monotonic() - start)

# Legacy function for backward compatibility (deprecated)
def _read_all(dir_path: Path, prefix: str) -> pd.DataFrame:
    """Legacy: Read all files with prefix. Use _read_latest_parquet instead."""
    files = sorted(dir_path.glob(f"{prefix}*.parquet"))
    return pd.concat([pd.read_parquet(f, engine="pyarrow") for f in files], ignore_index=True) if files else pd.DataFrame()

def restore_from_backups(backup_dir: Optional[Path] = None) -> RestoreResult:
    """Legacy restoration function - deprecated but kept for compatibility."""
    start_time = time.monotonic()
    config = BackupConfig.from_global_config() if backup_dir is None else BackupConfig(True, backup_dir, 1000, "zstd")
    
    a_df = _read_all(config.backup_dir / "articles", "articles_")
    v_df = _read_all(config.backup_dir / "vectors", "vectors_")
    
    if a_df.empty:
        return RestoreResult(success=False, restored_counts={}, duration_seconds=0.0, error="No files")

    # Legacy logic for scraped_articles only
    a_df = a_df.sort_values("updated_at", ascending=False).drop_duplicates(subset=["normalized_url"], keep="first")
    kept_rowids = set(a_df["vec_rowid"].tolist())
    if not v_df.empty: v_df = v_df[v_df["rowid"].isin(kept_rowids)]

    restored_counts = {}

    for _, r in a_df.iterrows():
        enqueue_write("""
            INSERT INTO scraped_articles (id, vec_rowid, normalized_url, url, title, conclusion, summary, metadata_json, embedding_status, scraped_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(normalized_url) DO UPDATE SET
                url=excluded.url, title=excluded.title, conclusion=excluded.conclusion, summary=excluded.summary,
                embedding_status=excluded.embedding_status, updated_at=excluded.updated_at
        """, (r["id"], r["vec_rowid"], r["normalized_url"], r["url"], r.get("title", ""), r.get("conclusion", ""), r.get("summary", ""), r.get("metadata_json", "{}"), r.get("embedding_status", "EMBEDDED"), r["scraped_at"], r["updated_at"]))
        restored_counts["scraped_articles"] = restored_counts.get("scraped_articles", 0) + 1

    for _, r in v_df.iterrows():
        b = bytes(r["embedding"]) if isinstance(r["embedding"], memoryview) else r["embedding"]
        enqueue_write("INSERT OR REPLACE INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)", (r["rowid"], b))
        restored_counts["scraped_articles_vec"] = restored_counts.get("scraped_articles_vec", 0) + 1

    enqueue_execscript("INSERT INTO scraped_articles_fts(scraped_articles_fts) VALUES('rebuild');")
    
    import asyncio
    try:
        asyncio.run(wait_for_writes())
    except RuntimeError:
        pass

    return RestoreResult(success=True, restored_counts=restored_counts, duration_seconds=time.monotonic() - start_time)
