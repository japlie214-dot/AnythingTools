# tools/backup/storage.py
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from tools.backup.config import BackupConfig
from tools.backup.schema import TABLE_SCHEMAS
from tools.backup.models import Watermark, ExportResult
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def read_watermark(config: BackupConfig) -> Watermark:
    if not config.watermark_path().exists(): return Watermark()
    try:
        with open(config.watermark_path(), "r", encoding="utf-8") as f: return Watermark(**json.load(f))
    except Exception:
        return Watermark()

def write_watermark(watermark: Watermark, config: BackupConfig) -> None:
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    temp_path = config.watermark_path().with_suffix(".tmp.json")
    try:
        with open(temp_path, "w", encoding="utf-8") as f: json.dump(watermark.dict(), f, indent=2, default=str)
        temp_path.replace(config.watermark_path())
    except Exception:
        if temp_path.exists(): temp_path.unlink()
        raise

def write_table_batch(table_name: str, chunks_iter, config: BackupConfig) -> int:
    """Consumes the DataFrame chunks iterator, writing them to a Parquet file atomically."""
    schema = TABLE_SCHEMAS.get(table_name)
    if schema is None:
        raise ValueError(f"No Parquet schema defined for table: {table_name}")
        
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    dest = config.table_dir(table_name) / f"{table_name}_{ts}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest.with_suffix(".tmp.parquet")
    
    total_written = 0
    try:
        writer = None
        for df, count in chunks_iter:
            if count == 0:
                continue
            table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(str(temp_path), schema, compression=config.compression, version="2.6")
            writer.write_table(table)
            total_written += count
            
        if writer is not None:
            writer.close()
            temp_path.replace(dest)
            log.dual_log(tag="Backup:Storage", level="INFO", message=f"Wrote {total_written} rows to {dest.name}")
        elif temp_path.exists():
            temp_path.unlink()
            
        return total_written
    except Exception:
        if 'writer' in locals() and writer is not None:
            try: writer.close()
            except Exception: pass
        if temp_path.exists(): temp_path.unlink()
        raise

def list_backup_files(config: BackupConfig) -> Tuple[int, Dict[str, int]]:
    counts: Dict[str, int] = {}
    total_size = 0
    for t in TABLE_SCHEMAS.keys():
        files = list(config.table_dir(t).glob(f"{t}_*.parquet"))
        counts[t] = len(files)
        total_size += sum(f.stat().st_size for f in files)
    return total_size, counts

def export_all_tables(conn, config: Optional[BackupConfig] = None, mode: str = "full") -> ExportResult:
    """Exports master tables. Mode 'full' generates a complete snapshot. Mode 'delta' only new/updated rows."""
    from tools.backup.exporter import export_table_chunks
    start = time.monotonic()
    if config is None: config = BackupConfig.from_global_config()
    if not config.enabled: return ExportResult(success=False, error="Disabled")

    config.ensure_dirs()
    wm = read_watermark(config)
    total_counts: Dict[str, int] = {}
    
    try:
        current_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for table_name in TABLE_SCHEMAS.keys():
            last_ts = wm.table_watermarks.get(table_name, "") if mode == "delta" else ""
            chunks = export_table_chunks(conn, table_name, config, mode=mode, last_ts=last_ts)
            written = write_table_batch(table_name, chunks, config)
            if written > 0:
                total_counts[table_name] = written
                wm.table_watermarks[table_name] = current_ts

        # If a full backup was run, we can safely delete older parquet files for these tables
        if mode == "full":
            for table_name in TABLE_SCHEMAS.keys():
                files = sorted(config.table_dir(table_name).glob(f"{table_name}_*.parquet"))
                # Keep only the newest one we just wrote
                if len(files) > 1:
                    for f in files[:-1]:
                        try: f.unlink()
                        except Exception: pass

        wm.last_export_ts = current_ts
        wm.total_articles_exported += total_counts.get("scraped_articles", 0)
        wm.total_vectors_exported += (total_counts.get("scraped_articles_vec", 0) + total_counts.get("long_term_memories_vec", 0))
        write_watermark(wm, config)
        
        return ExportResult(success=True, exported_counts=total_counts, duration_seconds=time.monotonic() - start)
    except Exception as e:
        log.dual_log(tag="Backup:Export", message=f"Failed: {e}", level="ERROR", exc_info=e)
        return ExportResult(success=False, error=str(e), duration_seconds=time.monotonic() - start)

# Legacy functions for backward compatibility (now unused but kept for reference)
def _write_parquet_atomic(df: pd.DataFrame, schema: pa.Schema, dest_path: Path, compression: str) -> int:
    # This function is deprecated, using write_table_batch instead
    pass

def write_batch(articles_df: pd.DataFrame, vectors_df: Optional[pd.DataFrame], ts_from: str, ts_to: str, config: BackupConfig) -> Tuple[str, Optional[str]]:
    # This function is deprecated, using export_all_tables instead
    pass

def export_delta(config: Optional[BackupConfig] = None) -> ExportResult:
    # This function is deprecated, using export_all_tables with mode="delta" instead
    pass
