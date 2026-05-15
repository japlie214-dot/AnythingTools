# tools/scraper/persistence.py
"""Article result parsing, database persistence, and AI curation helpers."""

import re
import json
import struct
import config
from utils.id_generator import ULID
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


def _parse_article_result(raw_result: dict, url: str) -> dict:
    """Parse JSON structured output into fields."""
    if raw_result.get("status") != "SUCCESS":
        return raw_result

    parsed_json = raw_result.get("parsed_json", {})
    if not parsed_json:
        return {"status": "FAILED", "reason": "Empty parsed_json content"}

    title = str(parsed_json.get("title") or "").strip()
    conclusion = str(parsed_json.get("conclusion") or "").strip()
    summary_raw = parsed_json.get("summary", [])

    if isinstance(summary_raw, list):
        summary = "\n".join(f"- {item}" for item in summary_raw if item)
    else:
        summary = str(summary_raw or "").strip()

    if not title or not conclusion:
        return {"status": "FAILED", "reason": "Missing mandatory JSON fields (title or conclusion)"}

    ulid_str = ULID.generate()
    _id_raw = int.from_bytes(ulid_str[:8].encode('utf-8'), 'big')
    # modulo 0x7FFFFFFFFFFFFFFE ensures value is strictly between 1 and 2^63-2, preventing SQLite overflow
    _id = (_id_raw % 0x7FFFFFFFFFFFFFFE) + 1

    return {
        "status":         "SUCCESS",
        "url":            url,
        "id":             _id,
        "ulid":           ulid_str,
        "title":          title,
        "conclusion":     conclusion,
        "summary":        summary,
        "raw_summary":    json.dumps(parsed_json, ensure_ascii=False),
    }


def _sync_scraped_article_atomic(parsed_result: dict, job_id: str | None, meta_str: str, local_meta_json: str):
    """Write article metadata and embeddings directly to the unified SQLite + Parquet pipeline."""
    from database.articles import enqueue_article_write
    from utils.vector_search import validate_embedding_bytes
    from clients.snowflake_client import EmbeddingError
    
    if isinstance(local_meta_json, dict):
        local_meta_json = json.dumps(local_meta_json)

    embedding_bytes = None
    embedding_status = "PENDING" if parsed_result.get("status") == "SUCCESS" else "SKIPPED"
    embedding_success = False

    try:
        if parsed_result.get("status") == "SUCCESS":
            _t = parsed_result["title"]
            _c = parsed_result["conclusion"]
            _s = parsed_result.get("summary", "")
            _header = f"{_t}: {_c}: "
            _avail = 8000 - len(_header)
            content_for_embedding = _header + (_s[:_avail] if _avail > 0 else "")

            try:
                from utils.vector_search import generate_embedding_sync
                embedding_bytes = generate_embedding_sync(content_for_embedding)

                validate_embedding_bytes(embedding_bytes)
                embedding_status = "EMBEDDED"

                lm = json.loads(local_meta_json)
                lm["embedding_synced"] = True
                local_meta_json = json.dumps(lm)
                embedding_success = True

            except EmbeddingError as ee:
                log.dual_log(tag="Scraper:Embedding:Error", message=f"Embedding generation failed for {parsed_result['ulid']}: {ee}", level="WARNING", payload={"error_type": type(ee).__name__, "error": str(ee)})
            except TimeoutError as te:
                log.dual_log(tag="Scraper:Embedding:Timeout", message=f"Snowflake timeout for {parsed_result['ulid']}. Marking pending.", level="WARNING", payload={"error": str(te)})
            except Exception as e:
                log.dual_log(tag="Scraper:Embedding:Error", message=f"Unexpected embedding failure: {e}", level="WARNING", payload={"error_type": type(e).__name__, "error": str(e)})

        article_data = {
            "id": parsed_result["ulid"],
            "url": parsed_result["url"],
            "title": parsed_result["title"],
            "conclusion": parsed_result["conclusion"],
            "summary": parsed_result["summary"],
            "metadata_json": meta_str,
            "embedding_status": embedding_status,
            "vec_rowid": parsed_result["id"],
        }

        res = enqueue_article_write(
            article_data=article_data,
            embedding_bytes=embedding_bytes,
            job_id=job_id,
            item_metadata=meta_str,
            local_metadata=local_meta_json
        )
        return res.receipt, embedding_success

    except Exception as e:
        log.dual_log(tag="Scraper:Persistence:Error", message=f"Unified persist failed: {e}", level="ERROR", payload={"error": str(e)})
        return None, False
    