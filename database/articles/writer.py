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
    task = ArticleWriteTask(
        article_id=article_data["id"],
        url=article_data["url"],
        title=article_data["title"],
        conclusion=article_data["conclusion"],
        summary=article_data["summary"],
        metadata_json=article_data["metadata_json"],
        embedding_status=article_data["embedding_status"],
        vec_rowid=int(article_data["vec_rowid"]),
        embedding_bytes=embedding_bytes,
        job_id=job_id,
        item_metadata=item_metadata,
        local_metadata=local_metadata,
    )

    start_writer()

    try:
        receipt = None
        try:
            store = get_article_store()
            # Ensure unique vec_rowid before building statements to prevent collisions
            task.vec_rowid = store._ensure_unique_vec_rowid(task.vec_rowid, task.article_id)
            db_statements = task.to_upsert_statements()
            
            if db_statements:
                receipt = enqueue_transaction(db_statements, track=True)
                
            return ArticleWriteResult(
                success=True,
                article_id=task.article_id,
                receipt=receipt,
                embedding_success=(task.embedding_status == "EMBEDDED"),
            )
        except Exception as db_err:
            log.dual_log(
                tag="Article:Write:Error",
                level="WARNING",
                message=f"Article write failed: {db_err}",
                payload={"article_id": task.article_id, "error": str(db_err)},
            )
            return ArticleWriteResult(success=False, article_id=task.article_id, error=str(db_err))
    except Exception as e:
        return ArticleWriteResult(success=False, article_id=task.article_id, error=str(e))

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
