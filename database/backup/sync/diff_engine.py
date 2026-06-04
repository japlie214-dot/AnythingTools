# database/backup/sync/diff_engine.py
import sqlite3
from typing import Dict, Any

class DiffEngine:
    @staticmethod
    def compute_triad_deltas(op_conn: sqlite3.Connection, backup_conn: sqlite3.Connection, cloud_iter: Any, table_name: str) -> Dict[str, Any]:
        """Memory-safe 3-way Set Diff using in-memory SQLite."""
        mem_db = sqlite3.connect(":memory:")
        mem_db.executescript("""
            CREATE TABLE diff_op (pk TEXT PRIMARY KEY, hash TEXT, ts TEXT, completeness INT);
            CREATE TABLE diff_local (pk TEXT PRIMARY KEY, hash TEXT, ts TEXT, completeness INT);
            CREATE TABLE diff_cloud (pk TEXT PRIMARY KEY, hash TEXT, ts TEXT, completeness INT);
        """)
        
        # 1. Extract metadata from Operational
        op_cur = op_conn.cursor()
        pk_col = "id"
        try:
            op_cur.row_factory = sqlite3.Row
            for col in op_cur.execute(f"PRAGMA table_info({table_name})").fetchall():
                if col["pk"] > 0:
                    pk_col = col["name"]
                    break
        except Exception: pass

        updated_col = "updated_at" if "updated_at" in (op_cur.execute(f"PRAGMA table_info({table_name})").fetchall() if hasattr(op_cur, 'execute') else []) else "id"
        # Note: In a real implementation, we'd properly check for updated_at existence.

        cols = []
        try:
            cols = op_conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        except Exception:
            pass
        
        column_names = [col[1] for col in cols] if cols else []
        has_updated_at = "updated_at" in column_names

        op_data = op_conn.execute(f"SELECT {pk_col}, {'updated_at' if has_updated_at else 'NULL'} FROM {table_name}").fetchall()
        mem_db.executemany("INSERT INTO diff_op (pk, ts) VALUES (?, ?)",
                          [(r[0], r[1] or "1970-01-01T00:00:00") for r in op_data])

        # 2. Extract metadata from Local
        bk_cur = backup_conn.cursor()
        bk_data = bk_cur.execute(f"SELECT {pk_col}, updated_at FROM {table_name}").fetchall()
        mem_db.executemany("INSERT INTO diff_local (pk, ts) VALUES (?, ?)",
                          [(r[0], r[1]) for r in bk_data if r[1]])

        # 3. Extract metadata from Cloud (if enabled)
        cloud_disabled = cloud_iter is None
        if not cloud_disabled:
            # cloud_iter is expected to be an iterator of (pk, ts)
            cloud_data = []
            for row in cloud_iter:
                cloud_data.append(row)
            mem_db.executemany("INSERT INTO diff_cloud (pk, ts) VALUES (?, ?)", cloud_data)

        query = """
            SELECT
                COALESCE(o.pk, l.pk, c.pk) as pk,
                CASE WHEN o.pk IS NOT NULL THEN 1 ELSE 0 END as in_op,
                CASE WHEN l.pk IS NOT NULL THEN 1 ELSE 0 END as in_local,
                CASE WHEN ? THEN NULL WHEN c.pk IS NOT NULL THEN 1 ELSE 0 END as in_cloud
            FROM diff_op o
            FULL OUTER JOIN diff_local l ON o.pk = l.pk
            FULL OUTER JOIN diff_cloud c ON o.pk = c.pk
        """
        # SQLite doesn't actually support FULL OUTER JOIN.
        # In a production system, we would use a UNION of LEFT JOINs or a temporary set of all PKs.
        # For this implementation, we'll simulate the set logic using a UNION of PKs first.
        
        all_pks_query = """
            SELECT pk FROM diff_op
            UNION SELECT pk FROM diff_local
            UNION SELECT pk FROM diff_cloud
        """
        all_pks = mem_db.execute(all_pks_query).fetchall()
        
        op_only = []
        bk_only = []
        conflicts = []
        
        for (pk,) in all_pks:
            o = mem_db.execute("SELECT ts FROM diff_op WHERE pk=?", (pk,)).fetchone()
            l = mem_db.execute("SELECT ts FROM diff_local WHERE pk=?", (pk,)).fetchone()
            c = mem_db.execute("SELECT ts FROM diff_cloud WHERE pk=?", (pk,)).fetchone()
            
            in_op = o is not None
            in_local = l is not None
            in_cloud = c is not None if not cloud_disabled else False
            
            if in_op and not in_local and not in_cloud:
                op_only.append(pk)
            elif not in_op and in_local:
                bk_only.append(pk)
            elif in_op and in_local:
                if o[0] != l[0]:
                    conflicts.append({
                        "id": pk,
                        "operational_ts": o[0],
                        "backup_ts": l[0],
                        "cloud_ts": c[0] if c else l[0]
                    })
                    
        mem_db.close()
        return {"op_only": op_only, "bk_only": bk_only, "conflicts": conflicts, "pk_col": pk_col}
