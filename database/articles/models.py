# database/articles/models.py
from dataclasses import dataclass
from typing import Optional, Any

@dataclass
class ArticleWriteTask:
    article_id: str
    url: str
    title: str
    conclusion: str
    summary: str
    metadata_json: str
    embedding_status: str
    vec_rowid: int
    embedding_bytes: Optional[bytes] = None
    job_id: Optional[str] = None
    item_metadata: Optional[str] = None
    local_metadata: Optional[str] = None

    def to_upsert_statements(self) -> list[tuple[str, tuple]]:
        """Build SQLite statements for article upsert.
        Omits 'id = excluded.id' to prevent PK mutation on conflict.
        """
        statements = []
        insert_sql = """
            INSERT INTO scraped_articles (
                id, vec_rowid, url, title, conclusion, summary,
                metadata_json, embedding_status, scraped_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(url) DO UPDATE SET
                vec_rowid = excluded.vec_rowid,
                title = excluded.title,
                conclusion = excluded.conclusion,
                summary = excluded.summary,
                metadata_json = excluded.metadata_json,
                embedding_status = excluded.embedding_status,
                updated_at = CURRENT_TIMESTAMP
        """
        statements.append((
            insert_sql,
            (
                self.article_id, self.vec_rowid, self.url,
                self.title, self.conclusion, self.summary, self.metadata_json,
                self.embedding_status,
            )
        ))
        if self.embedding_bytes and self.embedding_status == "EMBEDDED":
            statements.append(("DELETE FROM scraped_articles_vec WHERE rowid = ?", (self.vec_rowid,)))
            statements.append(("INSERT INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)", (self.vec_rowid, self.embedding_bytes)))
            statements.append(("UPDATE scraped_articles SET embedding_status = 'EMBEDDED' WHERE id = ?", (self.article_id,)))
        
        if self.job_id and self.item_metadata and self.local_metadata:
            statements.append((
                """UPDATE job_items SET status = ?, output_data = ?, item_metadata = ?, updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND json_extract(item_metadata, '$.step') = json_extract(?, '$.step')
                AND json_extract(item_metadata, '$.ulid') = json_extract(?, '$.ulid')""",
                ("COMPLETED", self.local_metadata, self.item_metadata, self.job_id, self.item_metadata, self.item_metadata)
            ))
        return statements

    def to_store_meta(self) -> dict:
        """Convert to metadata dict for ArticleStore.upsert_article()."""
        from datetime import datetime, timezone
        return {
            "id": self.article_id,
            "url": self.url,
            "title": self.title,
            "conclusion": self.conclusion,
            "summary": self.summary,
            "metadata_json": self.metadata_json,
            "embedding_status": self.embedding_status,
            "vec_rowid": self.vec_rowid,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

@dataclass
class ArticleDeleteTask:
    """Task for deleting an article from the system."""
    article_id: str

@dataclass
class ArticleDeleteResult:
    """Result of an article delete operation."""
    success: bool
    article_id: str
    error: Optional[str] = None

@dataclass
class ArticleWriteResult:
    success: bool
    article_id: str
    receipt: Optional[Any] = None
    error: Optional[str] = None
    embedding_success: bool = False
