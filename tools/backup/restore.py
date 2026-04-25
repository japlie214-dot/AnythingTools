# tools/backup/restore.py
import time
from pathlib import Path
from typing import Optional
import pandas as pd
from database.writer import enqueue_write, enqueue_execscript, wait_for_writes
from tools.backup.config import BackupConfig
from tools.backup.models import RestoreResult
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def _read_all(dir_path: Path, prefix: str) -> pd.DataFrame:
    files = sorted(dir_path.glob(f"{prefix}*.parquet"))
    return pd.concat([pd.read_parquet(f, engine="pyarrow") for f in files], ignore_index=True) if files else pd.DataFrame()

def restore_from_backups(backup_dir: Optional[Path] = None) -> RestoreResult:
    start_time = time.monotonic()
    config = BackupConfig.from_global_config() if backup_dir is None else BackupConfig(True, backup_dir, 1000, "zstd")
    
    a_df = _read_all(config.articles_dir, "articles_")
    v_df = _read_all(config.vectors_dir, "vectors_")
    
    if a_df.empty:
        return RestoreResult(success=False, articles_restored=0, vectors_restored=0, files_processed=0, duration_seconds=0.0, error="No files")

    a_df = a_df.sort_values("updated_at", ascending=False).drop_duplicates(subset=["normalized_url"], keep="first")
    kept_rowids = set(a_df["vec_rowid"].tolist())
    if not v_df.empty: v_df = v_df[v_df["rowid"].isin(kept_rowids)]

    for _, r in a_df.iterrows():
        enqueue_write("""
            INSERT INTO scraped_articles (id, vec_rowid, normalized_url, url, title, conclusion, summary, metadata_json, embedding_status, scraped_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(normalized_url) DO UPDATE SET
                url=excluded.url, title=excluded.title, conclusion=excluded.conclusion, summary=excluded.summary,
                embedding_status=excluded.embedding_status, updated_at=excluded.updated_at
        """, (r["id"], r["vec_rowid"], r["normalized_url"], r["url"], r.get("title", ""), r.get("conclusion", ""), r.get("summary", ""), r.get("metadata_json", "{}"), r.get("embedding_status", "EMBEDDED"), r["scraped_at"], r["updated_at"]))

    for _, r in v_df.iterrows():
        b = bytes(r["embedding"]) if isinstance(r["embedding"], memoryview) else r["embedding"]
        enqueue_write("INSERT OR REPLACE INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)", (r["rowid"], b))

    enqueue_execscript("INSERT INTO scraped_articles_fts(scraped_articles_fts) VALUES('rebuild');")
    
    import asyncio
    try:
        asyncio.run(wait_for_writes())
    except RuntimeError:
        pass # Handle if already in event loop

    return RestoreResult(success=True, articles_restored=len(a_df), vectors_restored=len(v_df), files_processed=len(list(config.articles_dir.glob("articles_*.parquet"))), duration_seconds=time.monotonic() - start_time)
