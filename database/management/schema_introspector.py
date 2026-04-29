# database/management/schema_introspector.py

import re
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional

@dataclass(frozen=True)
class ColumnInfo:
    cid: int
    name: str
    type: str
    notnull: bool
    dflt_value: Optional[str]
    pk: int

def _get_columns(conn: sqlite3.Connection, table_name: str) -> List[ColumnInfo]:
    """Return columns via PRAGMA table_info."""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    return [ColumnInfo(*row) for row in cursor.fetchall()]

def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None

def trigger_exists(conn: sqlite3.Connection, trigger_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
        (trigger_name,),
    ).fetchone()
    return row is not None

def _normalize_type_affinity(type_str: str) -> str:
    """Normalize SQLite dynamic types to standard affinities to prevent false drift."""
    t = type_str.upper()
    if "INT" in t:
        return "INTEGER"
    if "CHAR" in t or "CLOB" in t or "TEXT" in t:
        return "TEXT"
    if "BLOB" in t or not t:
        return "BLOB"
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return "REAL"
    return "NUMERIC"

def _columns_from_ddl_in_memory(ddl: str, table_name: str) -> Optional[List[ColumnInfo]]:
    """Execute DDL against a temporary :memory: DB and introspect PRAGMA table_info."""
    try:
        with sqlite3.connect(":memory:") as mem:
            mem.executescript(ddl)
            return _get_columns(mem, table_name)
    except sqlite3.OperationalError:
        return None

def schema_matches(
    conn: sqlite3.Connection,
    table_name: str,
    desired_ddl: str,
    is_virtual: bool = False,
) -> bool:
    """Compare actual runtime schema against desired canonical DDL."""
    if not table_exists(conn, table_name):
        return False

    # For virtual tables (FTS5 / vec0), use existence-based reconciliation
    if is_virtual:
        return table_exists(conn, table_name)

    # For regular tables, compare effective column list via PRAGMA table_info.
    actual_cols = _get_columns(conn, table_name)
    desired_cols = _columns_from_ddl_in_memory(ddl=desired_ddl, table_name=table_name)

    if desired_cols is None:
        return True

    if len(actual_cols) != len(desired_cols):
        return False

    actual_map: Dict[str, ColumnInfo] = {c.name.lower(): c for c in actual_cols}
    desired_map: Dict[str, ColumnInfo] = {c.name.lower(): c for c in desired_cols}

    if set(actual_map.keys()) != set(desired_map.keys()):
        return False

    for name in desired_map:
        a = actual_map[name]
        d = desired_map[name]
        if _normalize_type_affinity(a.type) != _normalize_type_affinity(d.type):
            return False
        if a.notnull != d.notnull:
            return False
        if a.pk != d.pk:
            return False

    return True
