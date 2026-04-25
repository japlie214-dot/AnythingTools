# tools/backup/exporter.py
import sqlite3
import struct
from typing import Iterator, Tuple, Optional, Dict, Any, List
import pandas as pd
from database.connection import DatabaseManager
from utils.logger import get_dual_logger
from tools.backup.config import BackupConfig
from tools.backup.schema import FLOAT32_COUNT

log = get_dual_logger(__name__)
_VECTOR_STRUCT = struct.Struct(f"<{FLOAT32_COUNT}f")

def _unpack_vector_blob(blob: bytes) -> bytes:
    if len(blob) != _VECTOR_STRUCT.size:
        raise ValueError(f"Vector BLOB length mismatch: {len(blob)}")
    _VECTOR_STRUCT.unpack(blob)
    return blob

def _fetch_articles_batch(conn: sqlite3.Connection, watermark_ts: str, watermark_id: str, batch_size: int) -> List[Dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT
            id, vec_rowid, normalized_url, url, title, conclusion, summary,
            metadata_json, embedding_status, scraped_at, updated_at,
            CASE WHEN updated_at > scraped_at THEN 'UPDATE' ELSE 'INSERT' END AS operation
        FROM scraped_articles
        WHERE updated_at > ? OR (updated_at = ? AND id > ?)
        ORDER BY updated_at ASC, id ASC
        LIMIT ?
        """,
        (watermark_ts, watermark_ts, watermark_id, batch_size)
    )
    return [dict(row) for row in cursor.fetchall()]

def _fetch_vectors(conn: sqlite3.Connection, vec_rowids: List[int]) -> List[Dict[str, Any]]:
    if not vec_rowids: return []
    placeholders = ",".join("?" * len(vec_rowids))
    try:
        cursor = conn.execute(f"SELECT rowid, embedding FROM scraped_articles_vec WHERE rowid IN ({placeholders})", tuple(vec_rowids))
    except sqlite3.OperationalError:
        return []
    
    vectors = []
    for row in cursor.fetchall():
        try:
            vectors.append({"rowid": row["rowid"], "embedding": _unpack_vector_blob(row["embedding"]), "article_id": None})
        except ValueError:
            continue
    return vectors

def export_delta_batches(config: Optional[BackupConfig] = None, watermark_ts: str = "", watermark_id: str = "") -> Iterator[Tuple[pd.DataFrame, Optional[pd.DataFrame], str, str]]:
    if config is None: config = BackupConfig.from_global_config()
    conn = DatabaseManager.get_read_connection()
    conn.row_factory = sqlite3.Row

    cur_ts, cur_id = watermark_ts, watermark_id
    while True:
        articles = _fetch_articles_batch(conn, cur_ts, cur_id, config.batch_size)
        if not articles: break

        new_ts, new_id = articles[-1]["updated_at"], articles[-1]["id"]
        vec_rowids = [a["vec_rowid"] for a in articles]
        vectors = _fetch_vectors(conn, vec_rowids)
        
        rowid_to_id = {a["vec_rowid"]: a["id"] for a in articles}
        for v in vectors: v["article_id"] = rowid_to_id.get(v["rowid"], "")

        yield pd.DataFrame(articles), (pd.DataFrame(vectors) if vectors else None), new_ts, new_id
        cur_ts, cur_id = new_ts, new_id
