# database/backup/sync/diff_engine.py
import sqlite3
from typing import Dict, Any

def _insert_diff_rows(mem_db: sqlite3.Connection, table: str, rows: list, pk_col) -> None:
    """Insert rows into an in-memory diff table, handling composite PKs.
    
    For single-PK tables, pk_col is a string and the PK is r[0].
    For composite-PK tables, pk_col is a list and the PK is the first
    N columns joined with '|'.
    
    Uses INSERT OR IGNORE to handle duplicate PKs gracefully (keeps the
    first occurrence). This prevents UNIQUE constraint violations when
    multiple rows share the same first PK column value in a composite-PK
    table where the old introspect_table_columns returned only the last
    PK column.
    """
    pk_count = len(pk_col) if isinstance(pk_col, (list, tuple)) else 1
    mem_rows = []
    for r in rows:
        if pk_count > 1:
            pk_str = "|".join(str(r[i]) for i in range(pk_count))
            hash_val = str(r[pk_count] or "")
            ts_val = str(r[pk_count + 1] or "")
        else:
            pk_str = str(r[0])
            hash_val = str(r[1] or "")
            ts_val = str(r[2] or "")
        mem_rows.append((pk_str, hash_val, ts_val))
    mem_db.executemany(
        f"INSERT OR IGNORE INTO {table} (pk, content_hash, ts) VALUES (?, ?, ?)",
        mem_rows,
    )

def _safe_ts_compare(ts1: str, ts2: str) -> int:
    """Returns 1 if ts1 > ts2, -1 if ts1 < ts2, 0 if equal. Safely parses ISO8601 strings."""
    from datetime import datetime
    def parse_ts(t: str) -> float:
        if not t: return 0.0
        t_clean = t.replace("Z", "+00:00").replace(" ", "T")
        try:
            return datetime.fromisoformat(t_clean).timestamp()
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
                try:
                    return datetime.strptime(t, fmt).timestamp()
                except ValueError:
                    continue
            return 0.0
    val1, val2 = parse_ts(ts1), parse_ts(ts2)
    if val1 > val2: return 1
    if val1 < val2: return -1
    return 0

class DiffEngine:
    @staticmethod
    def compute_deltas(op_conn: sqlite3.Connection, cloud_conn: sqlite3.Connection, table_name: str, pk_col=None) -> Dict[str, Any]:
        """Purely computational 2-way Set Diff using in-memory SQLite.

        Strategy:
        - Populate lightweight in-memory tables with pk, content_hash, and
          updated_at timestamps (if the column exists).
        - Perform a single LEFT JOIN pass (via UNION trick) to classify rows.
        
        IMPORTANT: This function is strictly computational. It does NOT modify
        either database. Content hash backfilling must be done BEFORE calling
        this function by the orchestrator (SyncEngine).
        """
        from database.backup.engine.sync_operations import _detect_pk_columns

        mem_db = sqlite3.connect(":memory:")
        mem_db.executescript("""
            CREATE TABLE diff_op (pk TEXT PRIMARY KEY, content_hash TEXT DEFAULT '', ts TEXT DEFAULT '');
            CREATE TABLE diff_cloud (pk TEXT PRIMARY KEY, content_hash TEXT DEFAULT '', ts TEXT DEFAULT '');
        """)
        
        # Use provided pk_col or detect from PRAGMA table_info
        if pk_col is None:
            pk_col, has_content_hash_op = _detect_pk_columns(op_conn, table_name)
        else:
            _, has_content_hash_op = _detect_pk_columns(op_conn, table_name)

        hash_col_op = "content_hash" if has_content_hash_op else "''"
        
        # Check if updated_at column exists — not all tables have it
        has_updated_at = False
        try:
            cols_info = op_conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            has_updated_at = any(c[1] == "updated_at" for c in cols_info)
        except Exception:
            pass
        ts_col = "updated_at" if has_updated_at else "''"
        
        try:
            # Build SELECT for PK columns
            if isinstance(pk_col, (list, tuple)):
                pk_select = ", ".join(pk_col)
            else:
                pk_select = pk_col
            op_rows = op_conn.execute(f"SELECT {pk_select}, {hash_col_op}, {ts_col} FROM {table_name}").fetchall()
        except Exception:
            op_rows = []
        _insert_diff_rows(mem_db, "diff_op", op_rows, pk_col)

        from database.backup.sync.helpers import introspect_table_columns
        _, _, has_content_hash_cloud = introspect_table_columns(cloud_conn, table_name)

        hash_col_cloud = "content_hash" if has_content_hash_cloud else "''"
        try:
            cloud_rows = cloud_conn.execute(f"SELECT {pk_select}, {hash_col_cloud}, {ts_col} FROM {table_name}").fetchall()
        except Exception:
            cloud_rows = []
        _insert_diff_rows(mem_db, "diff_cloud", cloud_rows, pk_col)
        result = mem_db.execute("""
            SELECT COALESCE(o.pk, c.pk) as pk,
                   CASE WHEN o.pk IS NOT NULL THEN 1 ELSE 0 END as in_op,
                   CASE WHEN c.pk IS NOT NULL THEN 1 ELSE 0 END as in_cloud,
                   o.content_hash as op_hash, c.content_hash as cloud_hash,
                   o.ts as op_ts, c.ts as cloud_ts
            FROM diff_op o LEFT JOIN diff_cloud c ON o.pk = c.pk
            UNION ALL
            SELECT c.pk as pk, 0 as in_op, 1 as in_cloud,
                   NULL as op_hash, c.content_hash as cloud_hash,
                   NULL as op_ts, c.ts as cloud_ts
            FROM diff_cloud c LEFT JOIN diff_op o ON c.pk = o.pk WHERE o.pk IS NULL
        """).fetchall()

        op_only, cloud_only, content_identical, timestamp_drift, genuine_conflicts = [], [], [], [], []
        for row in result:
            pk, in_op, in_cloud, op_hash, cloud_hash, op_ts, cloud_ts = row
            if in_op and not in_cloud:
                op_only.append(pk)
            elif in_cloud and not in_op:
                cloud_only.append(pk)
            elif in_op and in_cloud:
                if has_content_hash_op and has_content_hash_cloud and op_hash and cloud_hash:
                    if op_hash == cloud_hash:
                        content_identical.append(pk)
                        if _safe_ts_compare(op_ts, cloud_ts) != 0:
                            timestamp_drift.append({"id": pk, "op_ts": op_ts, "cloud_ts": cloud_ts, "classification": "timestamp_drift"})
                    else:
                        genuine_conflicts.append({"id": pk, "op_ts": op_ts, "cloud_ts": cloud_ts, "op_hash": op_hash, "cloud_hash": cloud_hash, "classification": "genuine_conflict"})
                else:
                    if _safe_ts_compare(op_ts, cloud_ts) != 0:
                        genuine_conflicts.append({"id": pk, "op_ts": op_ts, "cloud_ts": cloud_ts, "classification": "legacy_timestamp_conflict"})
                    else:
                        content_identical.append(pk)

        mem_db.close()
        total_rows = len(op_only) + len(cloud_only) + len(content_identical)
        return {
            "pk_col": pk_col,
            "op_only": op_only,
            "cloud_only": cloud_only,
            "content_identical": content_identical,
            "timestamp_drift": timestamp_drift,
            "genuine_conflicts": genuine_conflicts,
            "total_rows": total_rows,
            "op_rows": len(op_rows),
            "cloud_rows": len(cloud_rows),
            "op_newer": sum(1 for d in timestamp_drift if _safe_ts_compare(d["op_ts"], d["cloud_ts"]) > 0),
            "cloud_newer": sum(1 for d in timestamp_drift if _safe_ts_compare(d["cloud_ts"], d["op_ts"]) > 0)
        }
