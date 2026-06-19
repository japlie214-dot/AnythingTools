# database/articles/store.py
import random
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Any

from database.writer import enqueue_transaction
from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


class ArticleStore:
    def __init__(self, backup_dir: Optional[str | Path] = None):
        pass

    def _ensure_unique_vec_rowid(self, vec_rowid: int, article_id: str) -> int:
        """Ensure vec_rowid does not collide with a different article in SQLite."""
        import sqlite3
        from database.connection import DB_PATH
        
        # Use an isolated connection to prevent closing the thread-local connection
        # cached by upstream legacy loops (like the scraper task).
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            current_rowid = vec_rowid
            attempts = 0
            while attempts < 10:
                row = conn.execute("SELECT id FROM scraped_articles WHERE vec_rowid = ?", (current_rowid,)).fetchone()
                if not row or row["id"] == article_id:
                    return current_rowid
                
                log.dual_log(
                    tag="Article:Store:Collision",
                    level="WARNING",
                    message=f"vec_rowid collision detected for {article_id}. Regenerating.",
                    payload={"colliding_rowid": current_rowid, "existing_article": row["id"]}
                )
                # Re-generate deterministic but randomized fallback
                current_rowid = random.randint(1, 0x7FFFFFFFFFFFFFFE)
                attempts += 1
                
            raise RuntimeError(f"Failed to find unique vec_rowid for {article_id} after 10 attempts.")
        finally:
            conn.close()

    # ── Public API: Upsert ───────────────────────────────────────────────

    def upsert_article(
        self,
        article_id: str,
        meta: dict,
        embedding_bytes: Optional[bytes] = None,
        extra_statements: Optional[List[Tuple[str, tuple]]] = None,
    ) -> Optional[Any]:
        """Create or update an article with SQLite writes and best-effort Cloud sync."""
        updated_at = meta.get("updated_at", datetime.now(timezone.utc).isoformat())
        meta["updated_at"] = updated_at

        # Resolve vec_rowid collisions
        vec_rowid = meta.get("vec_rowid")
        if vec_rowid is not None:
            vec_rowid = self._ensure_unique_vec_rowid(int(vec_rowid), article_id)
            meta["vec_rowid"] = vec_rowid

        # Compute content_hash for efficient cloud diff detection
        try:
            from database.backup.sync.foundation import ContentHasher
            content_hash = ContentHasher.compute_row_hash("scraped_articles", {**meta, "id": article_id})
        except Exception:
            content_hash = ""

        # Enqueue SQLite upsert
        embedding_status = meta.get("embedding_status", "PENDING")
        db_statements = self._build_upsert_statements(
            article_id, meta, embedding_bytes, embedding_status
        )
        if content_hash:
            db_statements.append(("UPDATE scraped_articles SET content_hash = ? WHERE id = ?", (content_hash, article_id)))
            
        if extra_statements:
            db_statements.extend(extra_statements)
            
        receipt = self.enqueue_tx(db_statements, track=True)
        
        # Enqueue Inline Cloud Sync
        try:
            from database.backup.writer.cloud_writer import enqueue_cloud_write
            scraped_at = meta.get("scraped_at", datetime.now(timezone.utc).isoformat())
            row_data = {
                "id": article_id,
                "url": meta.get("url", ""),
                "title": meta.get("title"),
                "conclusion": meta.get("conclusion"),
                "summary": meta.get("summary"),
                "metadata_json": meta.get("metadata_json", "{}"),
                "embedding_status": embedding_status,
                "vec_rowid": vec_rowid,
                "content_hash": content_hash,
                "scraped_at": scraped_at,
                "updated_at": updated_at
            }
            enqueue_cloud_write("scraped_articles", row_data, pk_col="id")
            # If we have a valid embedding, enqueue the vec backup record as well
            if embedding_bytes and embedding_status == "EMBEDDED" and vec_rowid is not None:
                vec_backup_data = {
                    "rowid": vec_rowid,
                    "article_id": article_id,
                    "embedding": embedding_bytes,
                    "hashed_at": updated_at
                }
                enqueue_cloud_write("scraped_articles_vec_backup", vec_backup_data, pk_col="rowid")
        except Exception:
            # Replace silent swallow with structured WARNING. The local
            # SQLite write has already succeeded (enqueued above), so this
            # failure only affects cloud sync. The periodic SyncEngine.sync_all()
            # will catch missed rows on the next cycle, but the operator
            # MUST be alerted that cloud sync was skipped for this article.
            import sys
            exc = sys.exc_info()[1]
            log.dual_log(
                tag="Article:Cloud:EnqueueFailed",
                level="WARNING",
                message=f"Cloud enqueue failed for article {article_id}: {exc}",
                payload={
                    "article_id": article_id,
                    "error": str(exc)[:300],
                    "error_type": type(exc).__name__,
                    "has_embedding": embedding_bytes is not None,
                },
            )

        log.dual_log(
            tag="Article:Store:Upsert",
            level="INFO",
            message=f"Upserted article {article_id}",
            payload={"article_id": article_id, "has_embedding": embedding_bytes is not None, "updated_at": updated_at},
        )
        return receipt

    def _build_upsert_statements(
        self,
        article_id: str,
        meta: dict,
        embedding_bytes: Optional[bytes],
        embedding_status: str,
    ) -> List[Tuple[str, tuple]]:
        """Build SQLite statements avoiding Primary Key updates on conflict."""
        statements = []
        vec_rowid = meta.get("vec_rowid")
        
        upsert_sql = """
            INSERT INTO scraped_articles (
                id, vec_rowid, url, title, conclusion, summary,
                metadata_json, embedding_status, scraped_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                vec_rowid = excluded.vec_rowid,
                title = excluded.title,
                conclusion = excluded.conclusion,
                summary = excluded.summary,
                metadata_json = excluded.metadata_json,
                embedding_status = excluded.embedding_status,
                updated_at = excluded.updated_at
        """
        statements.append((
            upsert_sql,
            (
                article_id,
                vec_rowid,
                meta.get("url", ""),
                meta.get("title"),
                meta.get("conclusion"),
                meta.get("summary"),
                meta.get("metadata_json", "{}"),
                embedding_status,
                meta.get("scraped_at", meta["updated_at"]),
                meta["updated_at"],
            ),
        ))

        if embedding_bytes and embedding_status == "EMBEDDED" and vec_rowid is not None:
            statements.append(("DELETE FROM scraped_articles_vec WHERE rowid = ?", (vec_rowid,)))
            statements.append(("INSERT INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)", (vec_rowid, embedding_bytes)))
            statements.append(("UPDATE scraped_articles SET embedding_status = 'EMBEDDED' WHERE id = ?", (article_id,)))
            statements.append((
                "INSERT OR REPLACE INTO scraped_articles_vec_backup (rowid, article_id, embedding, hashed_at) VALUES (?, ?, ?, ?)",
                (vec_rowid, article_id, embedding_bytes, meta["updated_at"])
            ))

        return statements

    # ── Public API: Delete ───────────────────────────────────────────────

    def delete_article(self, article_id: str) -> None:
        """Delete an article with SQLite deletion and best-effort Cloud sync."""
        # Enqueue SQLite delete
        self.enqueue_tx([("DELETE FROM scraped_articles WHERE id = ?", (article_id,))])
        
        # Enqueue Inline Cloud Sync
        try:
            from database.backup.writer.cloud_writer import enqueue_cloud_delete
            enqueue_cloud_delete("scraped_articles", article_id, pk_col="id")
        except Exception:
            import sys
            exc = sys.exc_info()[1]
            log.dual_log(
                tag="Article:Cloud:DeleteFailed",
                level="WARNING",
                message=f"Cloud delete failed for article {article_id}: {exc}",
                payload={
                    "article_id": article_id,
                    "error": str(exc)[:300],
                    "error_type": type(exc).__name__,
                },
            )

        log.dual_log(
            tag="Article:Store:Delete",
            level="INFO",
            message=f"Deleted article {article_id}",
            payload={"article_id": article_id},
        )

    # ── SQLite Queue Helper ──────────────────────────────────────────────

    @staticmethod
    def enqueue_tx(statements: List[Tuple[str, tuple]], track: bool = False) -> Optional[Any]:
        if statements:
            return enqueue_transaction(statements, track=track)
        return None


# ── Global Singleton ─────────────────────────────────────────────────────

_global_store: Optional[ArticleStore] = None
_global_store_lock = __import__("threading").Lock()

def get_article_store() -> ArticleStore:
    global _global_store
    with _global_store_lock:
        if _global_store is None:
            _global_store = ArticleStore()
        return _global_store
