# database/backup/sync/diff_engine.py
import sqlite3
from typing import Dict, Any

class DiffEngine:
    @staticmethod
    def compute_triad_deltas(op_conn: sqlite3.Connection, backup_conn: sqlite3.Connection, cloud_iter: Any, table_name: str) -> Dict[str, Any]:
        """Memory-safe 3-way Set Diff using in-memory SQLite with content_hash awareness.

        Strategy:
        - Populate lightweight in-memory tables with pk, content_hash, and updated_at timestamps.
        - Backfill missing content_hash values in the operational DB on-the-fly using ContentHasher.
        - Perform a single LEFT JOIN pass (via UNION trick) to classify rows into op_only, bk_only,
          content_identical, timestamp_drift and genuine_conflicts.
        - When content_hash is missing for either side, fall back to timestamp comparison.
        """
        from database.backup.sync.foundation import ContentHasher

        mem_db = sqlite3.connect(":memory:")
        mem_db.executescript("""
            CREATE TABLE diff_op (pk TEXT PRIMARY KEY, content_hash TEXT DEFAULT '', ts TEXT DEFAULT '');
            CREATE TABLE diff_local (pk TEXT PRIMARY KEY, content_hash TEXT DEFAULT '', ts TEXT DEFAULT '');
        """)
        
        pk_col = "id"
        has_content_hash_op = False
        try:
            cols = op_conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            for col in cols:
                if col[5] > 0: pk_col = col[1]
                if col[1] == "content_hash": has_content_hash_op = True
        except Exception:
            pass

        # Backfill missing content_hash in operational DB if column exists
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

        has_content_hash_bk = False
        try:
            bk_cols = backup_conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            for col in bk_cols:
                if col[1] == "content_hash": has_content_hash_bk = True
        except Exception:
            pass

        hash_col_bk = "content_hash" if has_content_hash_bk else "''"
        try:
            bk_rows = backup_conn.execute(f"SELECT {pk_col}, {hash_col_bk}, updated_at FROM {table_name}").fetchall()
        except Exception:
            bk_rows = []
        mem_db.executemany("INSERT INTO diff_local (pk, content_hash, ts) VALUES (?, ?, ?)", [(str(r[0]), str(r[1] or ""), str(r[2] or "")) for r in bk_rows])

        result = mem_db.execute("""
            SELECT COALESCE(o.pk, l.pk) as pk,
                   CASE WHEN o.pk IS NOT NULL THEN 1 ELSE 0 END as in_op,
                   CASE WHEN l.pk IS NOT NULL THEN 1 ELSE 0 END as in_bk,
                   o.content_hash as op_hash, l.content_hash as bk_hash,
                   o.ts as op_ts, l.ts as bk_ts
            FROM diff_op o LEFT JOIN diff_local l ON o.pk = l.pk
            UNION ALL
            SELECT l.pk as pk, 0 as in_op, 1 as in_bk,
                   NULL as op_hash, l.content_hash as bk_hash,
                   NULL as op_ts, l.ts as bk_ts
            FROM diff_local l LEFT JOIN diff_op o ON l.pk = o.pk WHERE o.pk IS NULL
        """).fetchall()

        op_only, bk_only, content_identical, timestamp_drift, genuine_conflicts = [], [], [], [], []
        for row in result:
            pk, in_op, in_bk, op_hash, bk_hash, op_ts, bk_ts = row
            if in_op and not in_bk:
                op_only.append(pk)
            elif in_bk and not in_op:
                bk_only.append(pk)
            elif in_op and in_bk:
                # Both sides present
                if has_content_hash_op and has_content_hash_bk and op_hash and bk_hash:
                    if op_hash == bk_hash:
                        content_identical.append(pk)
                        if op_ts != bk_ts:
                            timestamp_drift.append({"id": pk, "op_ts": op_ts, "bk_ts": bk_ts, "classification": "timestamp_drift"})
                    else:
                        genuine_conflicts.append({"id": pk, "op_ts": op_ts, "bk_ts": bk_ts, "op_hash": op_hash, "bk_hash": bk_hash, "classification": "genuine_conflict"})
                else:
                    # Fallback: timestamp comparison
                    if op_ts != bk_ts:
                        genuine_conflicts.append({"id": pk, "op_ts": op_ts, "bk_ts": bk_ts, "classification": "legacy_timestamp_conflict"})
                    else:
                        content_identical.append(pk)

        mem_db.close()
        total_rows = len(op_only) + len(bk_only) + len(content_identical)
        return {
            "pk_col": pk_col,
            "op_only": op_only,
            "bk_only": bk_only,
            "content_identical": content_identical,
            "timestamp_drift": timestamp_drift,
            "genuine_conflicts": genuine_conflicts,
            "total_rows": total_rows,
            "op_rows": len(op_rows),
            "bk_rows": len(bk_rows),
            "op_newer": sum(1 for d in timestamp_drift if d["op_ts"] > d["bk_ts"]),
            "bk_newer": sum(1 for d in timestamp_drift if d["bk_ts"] > d["op_ts"]) 
        }
