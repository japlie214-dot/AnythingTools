# database/backup/sync/diff_engine.py
import sqlite3
from typing import Dict, Any

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
    def compute_deltas(op_conn: sqlite3.Connection, cloud_conn: sqlite3.Connection, table_name: str) -> Dict[str, Any]:
        """Memory-safe 2-way Set Diff using in-memory SQLite with content_hash awareness.

        Strategy:
        - Populate lightweight in-memory tables with pk, content_hash, and updated_at timestamps.
        - Backfill missing content_hash values in the operational DB on-the-fly using ContentHasher.
        - Perform a single LEFT JOIN pass (via UNION trick) to classify rows.
        """
        from database.backup.sync.foundation import ContentHasher

        mem_db = sqlite3.connect(":memory:")
        mem_db.executescript("""
            CREATE TABLE diff_op (pk TEXT PRIMARY KEY, content_hash TEXT DEFAULT '', ts TEXT DEFAULT '');
            CREATE TABLE diff_cloud (pk TEXT PRIMARY KEY, content_hash TEXT DEFAULT '', ts TEXT DEFAULT '');
        """)
        
        from database.backup.sync.helpers import introspect_table_columns
        pk_col, _, has_content_hash_op = introspect_table_columns(op_conn, table_name)

        if has_content_hash_op:
            try:
                from database.connection import DatabaseManager
                write_conn = DatabaseManager.create_write_connection()
                try:
                    count = write_conn.execute(f"SELECT COUNT(*) FROM {table_name} WHERE content_hash = '' OR content_hash IS NULL").fetchone()[0]
                    if count > 0:
                        op_rows = write_conn.execute(f"SELECT * FROM {table_name} WHERE content_hash = '' OR content_hash IS NULL").fetchall()
                        col_names = [d[0] for d in write_conn.execute(f"SELECT * FROM {table_name} LIMIT 1").description]
                        for r in op_rows:
                            row_dict = dict(zip(col_names, r))
                            new_hash = ContentHasher.compute_row_hash(table_name, row_dict)
                            write_conn.execute(f"UPDATE {table_name} SET content_hash = ? WHERE {pk_col} = ?", (new_hash, row_dict[pk_col]))
                        write_conn.commit()
                finally:
                    write_conn.close()
            except Exception:
                pass

        hash_col_op = "content_hash" if has_content_hash_op else "''"
        try:
            op_rows = op_conn.execute(f"SELECT {pk_col}, {hash_col_op}, updated_at FROM {table_name}").fetchall()
        except Exception:
            op_rows = []
        mem_db.executemany("INSERT INTO diff_op (pk, content_hash, ts) VALUES (?, ?, ?)", [(str(r[0]), str(r[1] or ""), str(r[2] or "")) for r in op_rows])

        from database.backup.sync.helpers import introspect_table_columns
        _, _, has_content_hash_cloud = introspect_table_columns(cloud_conn, table_name)

        hash_col_cloud = "content_hash" if has_content_hash_cloud else "''"
        try:
            cloud_rows = cloud_conn.execute(f"SELECT {pk_col}, {hash_col_cloud}, updated_at FROM {table_name}").fetchall()
        except Exception:
            cloud_rows = []
        mem_db.executemany("INSERT INTO diff_cloud (pk, content_hash, ts) VALUES (?, ?, ?)", [(str(r[0]), str(r[1] or ""), str(r[2] or "")) for r in cloud_rows])

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
