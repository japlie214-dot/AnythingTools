# database/backup/sync/helpers.py
import sqlite3
from typing import Tuple, Union

def introspect_table_columns(conn: sqlite3.Connection, table_name: str) -> Tuple[Union[str, Tuple[str, ...]], str, bool]:
    """Inspect table columns. Returns (pk_col, hash_col, has_hash).
    
    pk_col is a string for single-PK tables, or a tuple of strings
    for composite PK tables. This matches the convention used by
    _detect_pk_columns in sync_operations.py.
    
    Per SQLite docs: https://www.sqlite.org/pragma.html#pragma_table_info
    PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk).
    The pk field is 0 for non-PK columns, or the 1-indexed position
    in the primary key for PK columns.
    """
    pk_cols_with_pos: list[tuple[int, str]] = []
    has_hash = False
    hash_col = "''"
    try:
        for col_info in conn.execute(f"PRAGMA table_info({table_name})").fetchall():
            if col_info[5] > 0:
                pk_cols_with_pos.append((col_info[5], col_info[1]))
            if col_info[1] == "content_hash":
                has_hash = True
                hash_col = "content_hash"
    except Exception:
        pass
    
    pk_cols_with_pos.sort()
    if pk_cols_with_pos:
        pk_col = pk_cols_with_pos[0][1] if len(pk_cols_with_pos) == 1 else tuple(n for _, n in pk_cols_with_pos)
    else:
        pk_col = "id"
    
    return pk_col, hash_col, has_hash

def normalize_cloud_row(row, columns: list) -> list:
    import datetime
    import struct
    from decimal import Decimal

    norm_row = []
    for val in row:
        if isinstance(val, (datetime.datetime, datetime.date)):
            norm_row.append(val.isoformat())
        elif isinstance(val, Decimal):
            norm_row.append(float(val))
        elif isinstance(val, list) and len(val) > 0 and isinstance(val[0], float):
            norm_row.append(struct.pack(f'<{len(val)}f', *val))
        else:
            norm_row.append(val)
    return norm_row
