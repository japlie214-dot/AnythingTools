# database/backup/vec/adapter.py
import sqlite3
from typing import List
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class VectorBackupAdapter:
    @classmethod
    def backup_vectors(cls, op_conn: sqlite3.Connection, backup_conn: sqlite3.Connection, delta_only: bool = True) -> int:
        try:
            rows = op_conn.execute("SELECT v.rowid, a.id, v.embedding FROM scraped_articles_vec v JOIN scraped_articles a ON a.vec_rowid = v.rowid").fetchall()
            if not rows:
                return 0

            if not delta_only:
                try:
                    backup_conn.execute("DELETE FROM scraped_articles_vec_backup")
                except Exception:
                    pass
                backup_conn.executemany("INSERT OR REPLACE INTO scraped_articles_vec_backup (rowid, article_id, embedding) VALUES (?, ?, ?)", [(r[0], r[1], bytes(r[2]) if r[2] is not None else None) for r in rows])
            else:
                existing_rowids = {r[0] for r in backup_conn.execute("SELECT rowid FROM scraped_articles_vec_backup").fetchall()}
                op_rowids = {r[0] for r in rows}

                new_rows = [(r[0], r[1], bytes(r[2]) if r[2] is not None else None) for r in rows if r[0] not in existing_rowids]
                deleted_rowids = existing_rowids - op_rowids

                if new_rows:
                    backup_conn.executemany("INSERT OR REPLACE INTO scraped_articles_vec_backup (rowid, article_id, embedding) VALUES (?, ?, ?)", new_rows)
                if deleted_rowids:
                    backup_conn.executemany("DELETE FROM scraped_articles_vec_backup WHERE rowid = ?", [(rid,) for rid in deleted_rowids])

                log.dual_log(
                    tag="Backup:Vec:Delta",
                    message=f"Vector delta: {len(new_rows)} new, {len(deleted_rowids)} deleted",
                    payload={"new": len(new_rows), "deleted": len(deleted_rowids)}
                )

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
            try:
                op_conn.execute("DELETE FROM scraped_articles_vec")
            except Exception:
                pass
            op_conn.executemany("INSERT INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)", [(r[0], bytes(r[1]) if r[1] is not None else None) for r in rows])
            return len(rows)
        except Exception as e:
            log.dual_log(tag="Restore:Vec:Error", message=f"Vector restore failed: {e}", level="ERROR", payload={"error": str(e)})
            try:
                op_conn.rollback()
            except Exception:
                pass
            return 0
        