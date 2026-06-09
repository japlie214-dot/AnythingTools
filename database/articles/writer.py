# database/articles/writer.py
from typing import Optional
from database.writer import WriteReceipt, enqueue_transaction, start_writer
from database.articles.models import ArticleWriteTask, ArticleWriteResult, ArticleDeleteTask, ArticleDeleteResult
from database.articles.store import get_article_store
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def enqueue_article_write(
    article_data: dict,
    embedding_bytes: Optional[bytes] = None,
    job_id: Optional[str] = None,
    item_metadata: Optional[str] = None,
    local_metadata: Optional[str] = None,
) -> ArticleWriteResult:
    """Legacy create-only API seamlessly backed by ArticleStore."""
    from datetime import datetime, timezone
    start_writer()

    try:
        store = get_article_store()
        article_id = article_data["id"]
        vec_rowid = int(article_data.get("vec_rowid", 0))
        vec_rowid = store._ensure_unique_vec_rowid(vec_rowid, article_id)
        
        meta = {
            "id": article_id,
            "url": article_data["url"],
            "title": article_data["title"],
            "conclusion": article_data["conclusion"],
            "summary": article_data["summary"],
            "metadata_json": article_data.get("metadata_json", "{}"),
            "embedding_status": article_data.get("embedding_status", "PENDING"),
            "vec_rowid": vec_rowid,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        
        extra_statements = []
        if job_id and item_metadata and local_metadata:
            extra_statements.append((
                """UPDATE job_items SET status = ?, output_data = ?, item_metadata = ?, updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND json_extract(item_metadata, '$.step') = json_extract(?, '$.step')
                AND json_extract(item_metadata, '$.ulid') = json_extract(?, '$.ulid')""",
                ("COMPLETED", local_metadata, item_metadata, job_id, item_metadata, item_metadata)
            ))

        receipt = store.upsert_article(
            article_id=article_id,
            meta=meta,
            embedding_bytes=embedding_bytes,
            extra_statements=extra_statements
        )
        
        return ArticleWriteResult(
            success=True,
            article_id=article_id,
            receipt=receipt,
            embedding_success=(meta["embedding_status"] == "EMBEDDED"),
        )
    except Exception as e:
        log.dual_log(
            tag="Article:Write:Error",
            level="WARNING",
            message=f"Article write failed: {e}",
            payload={"article_id": article_data.get("id"), "error": str(e)},
        )
        return ArticleWriteResult(success=False, article_id=article_data.get("id", ""), error=str(e))

def upsert_article(article_id: str, meta: dict, embedding_bytes: Optional[bytes] = None) -> ArticleWriteResult:
    start_writer()
    try:
        store = get_article_store()
        store.upsert_article(article_id, meta, embedding_bytes)
        return ArticleWriteResult(
            success=True,
            article_id=article_id,
            embedding_success=meta.get("embedding_status") == "EMBEDDED",
        )
    except Exception as e:
        log.dual_log(tag="Article:Upsert:Error", level="ERROR", message=f"Article upsert failed: {e}", payload={"error": str(e)})
        return ArticleWriteResult(success=False, article_id=article_id, error=str(e))

def delete_article(article_id: str) -> ArticleDeleteResult:
    start_writer()
    try:
        store = get_article_store()
        store.delete_article(article_id)
        return ArticleDeleteResult(success=True, article_id=article_id)
    except Exception as e:
        log.dual_log(tag="Article:Delete:Error", level="ERROR", message=f"Article delete failed: {e}", payload={"error": str(e)})
        return ArticleDeleteResult(success=False, article_id=article_id, error=str(e))
