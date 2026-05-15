# database/articles/schema.py
import datetime
from database.backup.schema import SCRAPED_ARTICLES_SCHEMA, SCRAPED_ARTICLES_VEC_SCHEMA

ARTICLE_SCHEMA = SCRAPED_ARTICLES_SCHEMA
VECTOR_SCHEMA = SCRAPED_ARTICLES_VEC_SCHEMA

def get_article_row_as_dict(task) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "id": task.article_id,
        "vec_rowid": task.vec_rowid,
        "url": task.url,
        "title": task.title,
        "conclusion": task.conclusion,
        "summary": task.summary,
        "metadata_json": task.metadata_json,
        "embedding_status": task.embedding_status,
        "scraped_at": now,
        "updated_at": now,
    }

def get_vector_row_as_dict(task) -> dict:
    return {
        "rowid": task.vec_rowid,
        "embedding": task.embedding_bytes,
    }
