# database/backup/sync/diff_engine.py
import sqlite3
from typing import Dict, Any

class DiffEngine:
    @staticmethod
    def compute_deltas(local_conn: sqlite3.Connection, table_name: str, cloud_metadata_iterator) -> Dict[str, Any]:
        cur = local_conn.cursor()
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS cloud_snap (id TEXT PRIMARY KEY, updated_at TEXT)")
        cur.execute("DELETE FROM cloud_snap")
        cur.executemany("INSERT INTO cloud_snap (id, updated_at) VALUES (?, ?)", cloud_metadata_iterator)
        pk = "id" if table_name != "scraped_articles_vec" else "rowid"
        updated_col = "updated_at" if table_name != "scraped_articles_vec" else "NULL"
        cur.execute(f"SELECT l.{pk} FROM {table_name} l LEFT JOIN cloud_snap c ON l.{pk} = c.id WHERE c.id IS NULL")
        local_only = [row[0] for row in cur.fetchall()]
        cur.execute(f"SELECT c.id FROM cloud_snap c LEFT JOIN {table_name} l ON c.id = l.{pk} WHERE l.{pk} IS NULL")
        cloud_only = [row[0] for row in cur.fetchall()]
        if updated_col != "NULL":
            cur.execute(f"SELECT l.{pk}, l.{updated_col} as local_ts, c.updated_at as cloud_ts FROM {table_name} l JOIN cloud_snap c ON l.{pk} = c.id WHERE l.{updated_col} != c.updated_at")
            conflicts = [{"id": row[0], "local_ts": row[1], "cloud_ts": row[2]} for row in cur.fetchall()]
        else:
            conflicts = []
        cur.execute("DROP TABLE cloud_snap")
        return {"local_only": local_only, "cloud_only": cloud_only, "conflicts": conflicts}
