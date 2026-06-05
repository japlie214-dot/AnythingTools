# database/backup/vec/vector_backup_adapter.py
import sqlite3
from typing import List, Optional
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class VectorBackupAdapter:
    @classmethod
    def backup_vectors(cls, op_conn: sqlite3.Connection, backup_conn: sqlite3.Connection) -> int:
        try:
            rows = op_conn.execute("SELECT v.rowid, a.id, v.embedding FROM scraped_articles_vec v JOIN scraped_articles a ON a.vec_rowid = v.rowid").fetchall()
            if not rows:
                return 0
            backup_conn.execute("DELETE FROM scraped_articles_vec_backup")
            backup_conn.executemany("INSERT OR REPLACE INTO scraped_articles_vec_backup (rowid, article_id, embedding) VALUES (?, ?, ?)", [(r[0], r[1], bytes(r[2])) for r in rows])
            backup_conn.commit()
            return len(rows)
        except Exception as e:
            log.dual_log(tag="Backup:Vec:Error", message=f"Vector backup failed: {e}", level="WARNING", payload={"error": str(e)})
            return 0

    @classmethod
    def restore_vectors(cls, backup_conn: sqlite3.Connection, op_conn: sqlite3.Connection) -> int:
        try:
            rows = backup_conn.execute("SELECT rowid, embedding FROM scraped_articles_vec_backup").fetchall()
            if not rows:
                return 0
            op_conn.execute("DELETE FROM scraped_articles_vec")
            op_conn.executemany("INSERT INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)", [(r[0], bytes(r[1])) for r in rows])
            return len(rows)
        except Exception as e:
            log.dual_log(tag="Restore:Vec:Error", message=f"Vector restore failed: {e}", level="ERROR", payload={"error": str(e)})
            op_conn.rollback()
            return 0
