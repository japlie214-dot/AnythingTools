# database/backup/vec/adapter.py
import sqlite3
from typing import List, Any

VECTOR_BYTES = 4096

def _validate_embedding_blob(blob: bytes, rowid: Any):
    if blob is not None and len(blob) != VECTOR_BYTES:
        raise ValueError(f"Invalid embedding BLOB for rowid={rowid}: expected {VECTOR_BYTES} bytes, got {len(blob)}")
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
                validated_rows = []
                for r in rows:
                    blob = bytes(r[2]) if r[2] is not None else None
                    if blob is not None:
                        try:
                            _validate_embedding_blob(blob, r[0])
                        except ValueError as e:
                            log.dual_log(tag="Backup:Vec:Validation", message=f"Skipping invalid vector backup: {e}", level="WARNING", payload={"rowid": r[0], "error": str(e)})
                            continue
                    validated_rows.append((r[0], r[1], blob))
                backup_conn.executemany("INSERT OR REPLACE INTO scraped_articles_vec_backup (rowid, article_id, embedding) VALUES (?, ?, ?)", validated_rows)
            else:
                existing_rowids = {r[0] for r in backup_conn.execute("SELECT rowid FROM scraped_articles_vec_backup").fetchall()}
                op_rowids = {r[0] for r in rows}

                new_rows = []
                for r in rows:
                    if r[0] not in existing_rowids:
                        blob = bytes(r[2]) if r[2] is not None else None
                        if blob is not None:
                            try:
                                _validate_embedding_blob(blob, r[0])
                            except ValueError as e:
                                log.dual_log(tag="Backup:Vec:Validation", message=f"Skipping invalid vector backup: {e}", level="WARNING", payload={"rowid": r[0], "error": str(e)})
                                continue
                        new_rows.append((r[0], r[1], blob))
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
            validated_rows = []
            for r in rows:
                blob = bytes(r[1]) if r[1] is not None else None
                if blob is not None:
                    try:
                        _validate_embedding_blob(blob, r[0])
                        validated_rows.append((r[0], blob))
                    except ValueError as e:
                        log.dual_log(tag="Restore:Vec:Validation", message=f"Skipping invalid vector restore: {e}", level="WARNING", payload={"rowid": r[0], "error": str(e)})
                else:
                    validated_rows.append((r[0], None))
            op_conn.executemany("INSERT INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)", validated_rows)
            return len(rows)
        except Exception as e:
            log.dual_log(tag="Restore:Vec:Error", message=f"Vector restore failed: {e}", level="ERROR", payload={"error": str(e)})
            try:
                op_conn.rollback()
            except Exception:
                pass
            return 0
        