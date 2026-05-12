# database/articles/writer.py
from typing import Optional
from database.writer import WriteReceipt, enqueue_transaction, start_writer
from database.articles.models import ArticleWriteTask, ArticleWriteResult
from database.articles.parquet_stream import get_streaming_writer
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def enqueue_article_write(
    article_data: dict,
    embedding_bytes: Optional[bytes] = None,
    job_id: Optional[str] = None,
    item_metadata: Optional[str] = None,
    local_metadata: Optional[str] = None,
) -> ArticleWriteResult:
    task = ArticleWriteTask(
        article_id=article_data["id"],
        normalized_url=article_data["normalized_url"],
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
        streaming_writer = get_streaming_writer()
        article_path = None
        
        try:
            article_path = streaming_writer.write_article(task)
            if task.embedding_bytes:
                streaming_writer.write_vector(task)
        except Exception as pq_err:
            log.dual_log(tag="Backup:Storage:Rollback", level="ERROR", message=f"Parquet write failed: {pq_err}", payload={"article_id": task.article_id, "error": str(pq_err)})
        
        db_statements = task.to_db_statements()
        
        try:
            receipt = enqueue_transaction(db_statements, track=True)
        except Exception as db_err:
            log.dual_log(tag="Backup:Storage:Rollback", level="ERROR", message=f"DB write failed: {db_err}. Orphaned Parquet row retained.", payload={"article_id": task.article_id, "error": str(db_err)})
            return ArticleWriteResult(success=False, article_id=task.article_id, error=str(db_err))
        
        return ArticleWriteResult(
            success=True,
            article_id=task.article_id,
            parquet_path=str(article_path) if article_path else None,
            receipt=receipt,
            embedding_success=(task.embedding_status == "EMBEDDED")
        )
        
    except Exception as e:
        log.dual_log(tag="Article:Write:Error", level="ERROR", message=f"Article write failed: {e}", payload={"article_id": task.article_id, "error": str(e)}, exc_info=e)
        return ArticleWriteResult(success=False, article_id=task.article_id, error=str(e))
