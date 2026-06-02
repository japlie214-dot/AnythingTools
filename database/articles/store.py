# database/articles/store.py
import json
import hashlib
import os
import tempfile
import random
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Tuple

from database.writer import enqueue_transaction
from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


class ArticleStore:
    def __init__(self, backup_dir: str | Path):
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
    ) -> None:
        """Create or update an article with SQLite writes."""
        updated_at = meta.get("updated_at", datetime.now(timezone.utc).isoformat())
        meta["updated_at"] = updated_at

        # Resolve vec_rowid collisions
        vec_rowid = meta.get("vec_rowid")
        if vec_rowid is not None:
            vec_rowid = self._ensure_unique_vec_rowid(int(vec_rowid), article_id)
            meta["vec_rowid"] = vec_rowid

        # Enqueue SQLite upsert
        embedding_status = meta.get("embedding_status", "PENDING")
        db_statements = self._build_upsert_statements(
            article_id, meta, embedding_bytes, embedding_status
        )
        self.enqueue_tx(db_statements)

        log.dual_log(
            tag="Article:Store:Upsert",
            level="INFO",
            message=f"Upserted article {article_id}",
            payload={"article_id": article_id, "has_embedding": embedding_bytes is not None, "updated_at": updated_at},
        )

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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
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
                meta["updated_at"],
            ),
        ))

        if embedding_bytes and embedding_status == "EMBEDDED" and vec_rowid is not None:
            statements.append(("DELETE FROM scraped_articles_vec WHERE rowid = ?", (vec_rowid,)))
            statements.append(("INSERT INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)", (vec_rowid, embedding_bytes)))
            statements.append(("UPDATE scraped_articles SET embedding_status = 'EMBEDDED' WHERE id = ?", (article_id,)))

        return statements

    # ── Public API: Delete ───────────────────────────────────────────────

    def delete_article(self, article_id: str) -> None:
        """Delete an article with SQLite deletion."""
        # Enqueue SQLite delete
        self.enqueue_tx([("DELETE FROM scraped_articles WHERE id = ?", (article_id,))])

        log.dual_log(
            tag="Article:Store:Delete",
            level="INFO",
            message=f"Deleted article {article_id}",
            payload={"article_id": article_id},
        )

    # ── SQLite Queue Helper ──────────────────────────────────────────────

    @staticmethod
    def enqueue_tx(statements: List[Tuple[str, tuple]]) -> None:
        if statements:
            enqueue_transaction(statements)


# ── Global Singleton ─────────────────────────────────────────────────────

_global_store: Optional[ArticleStore] = None
_global_store_lock = __import__("threading").Lock()

def get_article_store() -> ArticleStore:
    global _global_store
    with _global_store_lock:
        if _global_store is None:
            from database.backup.settings import BackupSettings
            from pathlib import Path
            settings = BackupSettings()
            backup_dir = Path(settings.local.db_path).parent / "articles"
            _global_store = ArticleStore(backup_dir)
        return _global_store
