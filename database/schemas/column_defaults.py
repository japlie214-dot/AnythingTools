"""database/schemas/column_defaults.py
Column auto-fill registry for schema evolution.

When a new column is added via ALTER TABLE, existing rows receive the
DDL-defined DEFAULT value. Computed columns (content_hash, embedding, etc.)
need computed values that go beyond static defaults.
"""
import hashlib
import sqlite3
from typing import Dict, Callable, Optional, Any

# Type for auto-fill functions: (conn, table_name, column_name) -> int (rows filled)
ColumnFillFunc = Callable[[sqlite3.Connection, str, str], int]

_REGISTRY: Dict[str, Dict[str, ColumnFillFunc]] = {}

def register(table_name: str, column_name: str, func: ColumnFillFunc) -> None:
    """Register an auto-fill function for a specific (table, column) pair."""
    _REGISTRY.setdefault(table_name.lower(), {})[column_name.lower()] = func

def get_filler(table_name: str, column_name: str) -> Optional[ColumnFillFunc]:
    """Look up auto-fill function. Returns None if no script registered."""
    return _REGISTRY.get(table_name.lower(), {}).get(column_name.lower())

def get_all_fillers_for_table(table_name: str) -> Dict[str, ColumnFillFunc]:
    """Return all registered auto-fill functions for a table."""
    return dict(_REGISTRY.get(table_name.lower(), {}))

def _fill_content_hash(conn: sqlite3.Connection, table_name: str, column_name: str) -> int:
    """Backfill content_hash for rows where it's empty or NULL using chunked executemany."""
    from database.backup.schema_registry import BackupSchemaRegistry
    from database.backup.sync.helpers import introspect_table_columns

    checksum_cols = BackupSchemaRegistry.get_checksum_columns(table_name)
    if not checksum_cols:
        return 0

    pk_col, _, _ = introspect_table_columns(conn, table_name)
    if not pk_col:
        return 0

    cursor = conn.execute(
        f"SELECT {pk_col}, {', '.join(checksum_cols)} FROM {table_name} "
        f"WHERE {column_name} = '' OR {column_name} IS NULL"
    )

    total_filled = 0
    while True:
        rows = cursor.fetchmany(5000)
        if not rows:
            break

        batch = []
        for row in rows:
            pk_val = row[0]
            parts = [str(v or "").strip() for v in row[1:]]
            concat = "||".join(parts)
            new_hash = hashlib.sha256(concat.encode("utf-8")).hexdigest()
            batch.append((new_hash, pk_val))

        conn.executemany(f"UPDATE {table_name} SET {column_name} = ? WHERE {pk_col} = ?", batch)
        total_filled += len(batch)

    return total_filled

def _fill_embedding_status(conn: sqlite3.Connection, table_name: str, column_name: str) -> int:
    """Backfill embedding_status with 'PENDING' for rows where it's empty."""
    from database.backup.sync.helpers import introspect_table_columns
    pk_col, _, _ = introspect_table_columns(conn, table_name)
    if not pk_col:
        return 0

    cursor = conn.execute(
        f"UPDATE {table_name} SET {column_name} = 'PENDING' "
        f"WHERE {column_name} = '' OR {column_name} IS NULL"
    )
    return cursor.rowcount

# -- Register all built-in fillers --
for _tbl in [
    "scraped_articles", "scraped_articles_vec_backup",
    "broadcast_batches", "broadcast_details",
    "sn_filings", "sn_notes", "sn_detail_registry", "sn_note_details"
]:
    register(_tbl, "content_hash", _fill_content_hash)

register("scraped_articles", "embedding_status", _fill_embedding_status)
