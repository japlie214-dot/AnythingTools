# database/backup/sync/diff_engine.py
import sqlite3
from typing import Dict, Any

class DiffEngine:
    @staticmethod
    def compute_triad_deltas(op_conn: sqlite3.Connection, backup_conn: sqlite3.Connection, table_name: str) -> Dict[str, Any]:
        """Computes differences between Operational and Backup (Local, previously synced from Cloud)."""
        op_cur = op_conn.cursor()
        bk_cur = backup_conn.cursor()
        
        # Dynamically discover the primary key column name
        pk_col = None
        try:
            # Set row_factory to sqlite3.Row to access by column name
            op_cur.row_factory = sqlite3.Row
            for col in op_cur.execute(f"PRAGMA table_info({table_name})").fetchall():
                if col["pk"] > 0:
                    pk_col = col["name"]
                    break
        except Exception:
            pk_col = "id"
            
        if not pk_col:
            pk_col = "id"
            
        updated_col = "updated_at"
        
        try:
            op_rows = {row[0]: row[1] for row in op_cur.execute(f"SELECT {pk_col}, {updated_col} FROM {table_name}").fetchall()}
            bk_rows = {row[0]: row[1] for row in bk_cur.execute(f"SELECT {pk_col}, {updated_col} FROM {table_name}").fetchall()}
        except sqlite3.OperationalError:
            # Handle cases where updated_at might be missing or pk_col is invalid
            return {"op_only": [], "bk_only": [], "conflicts": [], "pk_col": pk_col}
        
        op_only = []
        bk_only = []
        conflicts = []
        
        all_ids = set(op_rows.keys()).union(set(bk_rows.keys()))
        for rid in all_ids:
            if rid not in bk_rows:
                op_only.append(rid)
            elif rid not in op_rows:
                bk_only.append(rid)
            else:
                if op_rows[rid] != bk_rows[rid]:
                    conflicts.append({
                        "id": rid,
                        "operational_ts": op_rows[rid],
                        "backup_ts": bk_rows[rid],
                        "cloud_ts": bk_rows[rid]  # Cloud sync happens before this diff
                    })
                    
        return {"op_only": op_only, "bk_only": bk_only, "conflicts": conflicts, "pk_col": pk_col}
