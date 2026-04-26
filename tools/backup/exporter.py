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

from database.schemas import MASTER_TABLES, ALL_VEC_TABLES

def export_table_chunks(conn: sqlite3.Connection, table_name: str, config: BackupConfig, mode: str = "full", last_ts: str = ""):
    """Stream table data directly into DataFrames, respecting memory limits. Yields (DataFrame, count)."""
    if table_name not in MASTER_TABLES:
        raise ValueError(f"Cannot export non-master table: {table_name}")

    # Handle virtual tables explicitly since SELECT * doesn't include rowid
    if table_name in ALL_VEC_TABLES:
        query = f"SELECT rowid, embedding FROM {table_name}"
    elif table_name.endswith("_fts"):
        query = f"SELECT rowid, * FROM {table_name}"
    else:
        query = f"SELECT * FROM {table_name}"
    
    # Delta mode strictly handles append/update based on updated_at.
    if mode == "delta" and last_ts:
        # Check if table has updated_at column
        cursor = conn.execute(f"PRAGMA table_info({table_name})")
        cols = [r[1] for r in cursor.fetchall()]
        if "updated_at" in cols:
            query += f" WHERE updated_at > '{last_ts}'"
    
    chunk_iter = pd.read_sql_query(query, conn, chunksize=500)
    for chunk in chunk_iter:
        yield chunk, len(chunk)
