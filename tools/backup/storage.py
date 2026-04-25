# tools/backup/storage.py
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Tuple
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from tools.backup.config import BackupConfig
from tools.backup.schema import ARTICLES_SCHEMA, VECTORS_SCHEMA
from tools.backup.models import Watermark, ExportResult
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def _write_parquet_atomic(df: pd.DataFrame, schema: pa.Schema, dest_path: Path, compression: str) -> int:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_suffix(".tmp.parquet")
    try:
        table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
        pq.write_table(table, str(temp_path), compression=compression, row_group_size=len(df), version="2.6")
        temp_path.replace(dest_path)
        return dest_path.stat().st_size
    except Exception:
        if temp_path.exists(): temp_path.unlink()
        raise

def write_batch(articles_df: pd.DataFrame, vectors_df: Optional[pd.DataFrame], ts_from: str, ts_to: str, config: BackupConfig) -> Tuple[str, Optional[str]]:
    clean_from = ts_from.replace(':','').replace(' ', '_')
    clean_to = ts_to.replace(':','').replace(' ', '_')
    filename_base = f"{clean_from}_{clean_to}" if ts_from else f"0_{clean_to}"
    art_path = config.articles_dir / f"articles_{filename_base}.parquet"
    _write_parquet_atomic(articles_df, ARTICLES_SCHEMA, art_path, config.compression)
    
    vec_path = None
    if vectors_df is not None and not vectors_df.empty:
        v_path = config.vectors_dir / f"vectors_{filename_base}.parquet"
        _write_parquet_atomic(vectors_df, VECTORS_SCHEMA, v_path, config.compression)
        vec_path = str(v_path)
    return str(art_path), vec_path

def read_watermark(config: BackupConfig) -> Watermark:
    if not config.watermark_path.exists(): return Watermark()
    try:
        with open(config.watermark_path, "r", encoding="utf-8") as f: return Watermark(**json.load(f))
    except Exception:
        return Watermark()

def write_watermark(watermark: Watermark, config: BackupConfig) -> None:
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    temp_path = config.watermark_path.with_suffix(".tmp.json")
    try:
        with open(temp_path, "w", encoding="utf-8") as f: json.dump(watermark.dict(), f, indent=2, default=str)
        temp_path.replace(config.watermark_path)
    except Exception:
        if temp_path.exists(): temp_path.unlink()
        raise

def list_backup_files(config: BackupConfig) -> Tuple[int, int, int]:
    arts = list(config.articles_dir.glob("articles_*.parquet"))
    vecs = list(config.vectors_dir.glob("vectors_*.parquet"))
    return len(arts), len(vecs), sum(f.stat().st_size for f in arts + vecs)

def export_delta(config: Optional[BackupConfig] = None) -> ExportResult:
    from tools.backup.exporter import export_delta_batches
    start_time = time.monotonic()
    if config is None: config = BackupConfig.from_global_config()
    if not config.enabled: return ExportResult(success=False, articles_exported=0, vectors_exported=0, new_watermark="", duration_seconds=0.0, error="Disabled")
    
    config.ensure_dirs()
    wm = read_watermark(config)
    tot_art, tot_vec = 0, 0
    last_ts, last_id = wm.last_export_ts if wm.last_export_ts else "", wm.last_article_id
    
    try:
        for a_df, v_df, batch_ts, batch_id in export_delta_batches(config, last_ts, wm.last_article_id):
            write_batch(a_df, v_df, last_ts, batch_ts, config)
            tot_art += len(a_df)
            tot_vec += len(v_df) if v_df is not None else 0
            last_ts, last_id = batch_ts, batch_id
            
        new_wm = Watermark(
            last_article_id=last_id,
            last_export_ts=last_ts if last_ts else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            total_articles_exported=wm.total_articles_exported + tot_art,
            total_vectors_exported=wm.total_vectors_exported + tot_vec
        )
        write_watermark(new_wm, config)
        return ExportResult(success=True, articles_exported=tot_art, vectors_exported=tot_vec, new_watermark=last_id, duration_seconds=time.monotonic() - start_time)
    except Exception as e:
        log.dual_log(tag="Backup:Export", message=f"Failed: {e}", level="ERROR", exc_info=e)
        return ExportResult(success=False, articles_exported=tot_art, vectors_exported=tot_vec, new_watermark=last_id, duration_seconds=time.monotonic() - start_time, error=str(e))
