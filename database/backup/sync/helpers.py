# database/backup/sync/helpers.py
import sqlite3
from typing import Tuple

def introspect_table_columns(conn: sqlite3.Connection, table_name: str) -> Tuple[str, str, bool]:
    pk_col = "id"
    hash_col = "''"
    has_hash = False
    try:
        for col_info in conn.execute(f"PRAGMA table_info({table_name})").fetchall():
            if col_info[5] > 0:
                pk_col = col_info[1]
            if col_info[1] == "content_hash":
                has_hash = True
                hash_col = "content_hash"
    except Exception:
        pass
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
