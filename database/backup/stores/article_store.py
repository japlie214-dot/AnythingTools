# database/backup/stores/article_store.py
import json
import random
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from database.backup.base_store import JsonStore

class ArticleStore(JsonStore):
    entity_key = "id"
    manifest_entity_key = "articles"

    def _ensure_unique_vec_rowid(self, vec_rowid: int, article_id: str) -> int:
        import sqlite3
        from database.connection import DB_PATH
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            current_rowid = vec_rowid
            attempts = 0
            while attempts < 10:
                row = conn.execute("SELECT id FROM scraped_articles WHERE vec_rowid = ?", (current_rowid,)).fetchone()
                if not row or row["id"] == article_id:
                    return current_rowid
                current_rowid = random.randint(1, 0x7FFFFFFFFFFFFFFE)
                attempts += 1
            raise RuntimeError(f"Failed to find unique vec_rowid for {article_id}")
        finally:
            conn.close()

    def upsert_article(self, article_id: str, meta: dict, embedding_bytes: Optional[bytes] = None) -> None:
        vec_rowid = meta.get("vec_rowid")
        if vec_rowid is not None:
            meta["vec_rowid"] = self._ensure_unique_vec_rowid(int(vec_rowid), article_id)
        self.upsert_entity(article_id, meta, embedding_bytes)

    def delete_article(self, article_id: str) -> None:
        self.delete_entity(article_id)

    def load_article_for_reconciliation(self, article_id: str) -> Optional[Tuple[dict, Optional[bytes]]]:
        meta = self._read_json(article_id)
        if meta is None:
            return None
        emb = None
        bin_path = self.backup_dir / f"{article_id}.bin"
        if bin_path.exists() and meta.get("checksum"):
            raw = self._read_bin(article_id)
            if raw is not None:
                import hashlib
                expected_checksum = self.manifest["articles"].get(article_id, {}).get("checksum")
                if expected_checksum and hashlib.sha256(raw).hexdigest() != expected_checksum:
                    from utils.logger import get_dual_logger
                    get_dual_logger(__name__).dual_log(
                        tag="Article:Store:ChecksumMismatch",
                        level="WARNING",
                        message=f"Embedding checksum mismatch for {article_id}",
                        payload={"article_id": article_id},
                    )
                else:
                    emb = raw
        return meta, emb

    def build_upsert_statements(self, entity_id: str, meta: dict, embedding_bytes: Optional[bytes] = None) -> List[Tuple[str, tuple]]:
        statements = []
        vec_rowid = meta.get("vec_rowid")
        embedding_status = meta.get("embedding_status", "PENDING")

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
                entity_id, vec_rowid, meta.get("url", ""), meta.get("title"),
                meta.get("conclusion"), meta.get("summary"), meta.get("metadata_json", "{}"),
                embedding_status, meta["updated_at"]
            )
        ))

        if embedding_bytes and embedding_status == "EMBEDDED" and vec_rowid is not None:
            statements.append(("DELETE FROM scraped_articles_vec WHERE rowid = ?", (vec_rowid,)))
            statements.append(("INSERT INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)", (vec_rowid, embedding_bytes)))
            statements.append(("UPDATE scraped_articles SET embedding_status = 'EMBEDDED' WHERE id = ?", (entity_id,)))

        return statements

    def build_delete_statements(self, entity_id: str) -> List[Tuple[str, tuple]]:
        return [("DELETE FROM scraped_articles WHERE id = ?", (entity_id,))]

    def get_all_from_sqlite(self, conn) -> List[dict]:
        rows = conn.execute("SELECT * FROM scraped_articles").fetchall()
        return [dict(r) for r in rows]

_global_store: Optional[ArticleStore] = None
_global_store_lock = __import__("threading").Lock()

def get_article_store() -> ArticleStore:
    global _global_store
    with _global_store_lock:
        if _global_store is None:
            from database.backup.config import BackupConfig
            config = BackupConfig.from_global_config()
            _global_store = ArticleStore(config.backup_dir, "articles", "manifest.json")
        return _global_store
