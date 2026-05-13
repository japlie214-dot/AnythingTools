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
    """Manages per-article backup files, manifest, and SQLite coordination."""

    def __init__(self, backup_dir: Path):
        self.backup_dir = backup_dir / "articles"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.backup_dir.parent / "manifest.json"
        self.manifest = self._load_manifest()

    # ── Manifest Management ──────────────────────────────────────────────

    def _load_manifest(self) -> dict:
        """Load manifest from disk, resolving corruption with a fresh dict."""
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "articles" not in data:
                        data["articles"] = {}
                    if "last_synced_at" not in data:
                        data["last_synced_at"] = None
                    return data
            except (json.JSONDecodeError, OSError) as e:
                log.dual_log(
                    tag="Article:Store:ManifestCorrupt",
                    level="WARNING",
                    message=f"Manifest corrupt or unreadable, starting fresh: {e}",
                    payload={"path": str(self.manifest_path), "error": str(e)},
                )
        return {"articles": {}, "last_synced_at": None}

    def _save_manifest(self) -> None:
        """Atomic manifest write using tempfile → os.replace."""
        tmp_path = self.manifest_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.manifest, f, separators=(",", ":"), ensure_ascii=False)
            os.replace(tmp_path, self.manifest_path)
        except Exception as e:
            log.dual_log(
                tag="Article:Store:ManifestWrite",
                level="ERROR",
                message=f"Failed to write manifest: {e}",
                payload={"error": str(e)},
            )
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            raise

    # ── Atomic File Helpers ──────────────────────────────────────────────

    @staticmethod
    def _compute_checksum(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _atomic_write(path: Path, content: bytes, mode: str = "wb") -> None:
        """Atomic file write using tempfile → os.replace on same filesystem."""
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix=path.stem + "_"
        )
        try:
            with os.fdopen(fd, mode) as f:
                f.write(content)
            os.replace(tmp_path, str(path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise

    def _read_article_json(self, article_id: str) -> Optional[dict]:
        json_path = self.backup_dir / f"{article_id}.json"
        if not json_path.exists():
            return None
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return None

    def _read_embedding_bin(self, article_id: str) -> Optional[bytes]:
        bin_path = self.backup_dir / f"{article_id}.bin"
        if not bin_path.exists():
            return None
        try:
            return bin_path.read_bytes()
        except OSError:
            return None

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
        """Create or update an article with atomic file + manifest + SQLite writes."""
        updated_at = meta.get("updated_at", datetime.now(timezone.utc).isoformat())
        meta["updated_at"] = updated_at

        # Resolve vec_rowid collisions
        vec_rowid = meta.get("vec_rowid")
        if vec_rowid is not None:
            vec_rowid = self._ensure_unique_vec_rowid(int(vec_rowid), article_id)
            meta["vec_rowid"] = vec_rowid

        # 1. Write files atomically
        json_path = self.backup_dir / f"{article_id}.json"
        json_content = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._atomic_write(json_path, json_content)

        if embedding_bytes:
            meta["checksum"] = self._compute_checksum(embedding_bytes)
            bin_path = json_path.with_suffix(".bin")
            self._atomic_write(bin_path, embedding_bytes)
        else:
            bin_path = json_path.with_suffix(".bin")
            if bin_path.exists():
                try:
                    bin_path.unlink()
                except Exception:
                    pass
            meta.pop("checksum", None)

        # 2. Instant manifest update
        self.manifest["articles"][article_id] = {
            "updated_at": updated_at,
            "checksum": meta.get("checksum"),
        }
        self._save_manifest()

        # 3. Enqueue SQLite upsert
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
                id, vec_rowid, normalized_url, url, title, conclusion, summary,
                metadata_json, embedding_status, scraped_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(normalized_url) DO UPDATE SET
                vec_rowid = excluded.vec_rowid,
                url = excluded.url,
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
                meta.get("normalized_url", ""),
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
        """Delete an article with file cleanup + manifest removal + SQLite deletion."""
        json_path = self.backup_dir / f"{article_id}.json"
        bin_path = json_path.with_suffix(".bin")

        # 1. Remove files safely
        for p in [json_path, bin_path]:
            if p.exists():
                try:
                    p.unlink()
                except Exception as e:
                    pass

        # 2. Instant manifest update
        removed = self.manifest["articles"].pop(article_id, None)
        if removed is not None:
            self._save_manifest()

        # 3. Enqueue SQLite delete
        self.enqueue_tx([("DELETE FROM scraped_articles WHERE id = ?", (article_id,))])

        log.dual_log(
            tag="Article:Store:Delete",
            level="INFO",
            message=f"Deleted article {article_id}",
            payload={"article_id": article_id, "was_tracked": removed is not None},
        )

    # ── SQLite Queue Helper ──────────────────────────────────────────────

    @staticmethod
    def enqueue_tx(statements: List[Tuple[str, tuple]]) -> None:
        if statements:
            enqueue_transaction(statements)

    # ── Query Helpers ────────────────────────────────────────────────────

    def load_article_for_reconciliation(self, article_id: str) -> Optional[Tuple[dict, Optional[bytes]]]:
        meta = self._read_article_json(article_id)
        if meta is None:
            return None

        emb = None
        bin_path = self.backup_dir / f"{article_id}.bin"
        if bin_path.exists() and meta.get("checksum"):
            raw = self._read_embedding_bin(article_id)
            if raw is not None:
                expected_checksum = self.manifest["articles"].get(article_id, {}).get("checksum")
                if expected_checksum and self._compute_checksum(raw) != expected_checksum:
                    log.dual_log(
                        tag="Article:Store:ChecksumMismatch",
                        level="WARNING",
                        message=f"Embedding checksum mismatch for {article_id}",
                        payload={"article_id": article_id},
                    )
                else:
                    emb = raw
        return meta, emb

    def mark_synced(self) -> None:
        self.manifest["last_synced_at"] = datetime.now(timezone.utc).isoformat()
        self._save_manifest()


# ── Global Singleton ─────────────────────────────────────────────────────

_global_store: Optional[ArticleStore] = None
_global_store_lock = __import__("threading").Lock()

def get_article_store() -> ArticleStore:
    global _global_store
    with _global_store_lock:
        if _global_store is None:
            from database.backup.config import BackupConfig
            config = BackupConfig.from_global_config()
            _global_store = ArticleStore(config.backup_dir)
        return _global_store
