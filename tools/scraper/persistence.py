# tools/scraper/persistence.py
"""Article result parsing, database persistence, and AI curation helpers."""

import re
import json
import struct
import config
from utils.id_generator import ULID
from utils.text_processing import normalize_url
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
        "normalized_url": normalize_url(url),
        "id":             _id,
        "ulid":           ulid_str,
        "title":          title,
        "conclusion":     conclusion,
        "summary":        summary,
        "raw_summary":    json.dumps(parsed_json, ensure_ascii=False),
    }


def _sync_scraped_article_atomic(parsed_result: dict, job_id: str | None, meta_str: str, local_meta_json: str):
    """Combine article metadata, embedding, and job-items updates into a single transaction.

    Returns a WriteReceipt (or None) that allows the caller to wait for flush completion and detect timeout.
    """
    from database.writer import enqueue_transaction
    from utils.vector_search import validate_embedding_bytes
    from clients.snowflake_client import EmbeddingError
    
    # Coerce to JSON string immediately to guarantee safety across all paths
    if isinstance(local_meta_json, dict):
        local_meta_json = json.dumps(local_meta_json)

    statements = []
    try:

        # Base article insert/update
        insert_sql = (
            """
            INSERT INTO scraped_articles (
                id, normalized_url, url, title, conclusion, summary, metadata_json,
                embedding_status, vec_rowid, scraped_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(normalized_url) DO UPDATE SET
                url = excluded.url,
                title = excluded.title,
                conclusion = excluded.conclusion,
                summary = excluded.summary,
                embedding_status = excluded.embedding_status,
                updated_at = CURRENT_TIMESTAMP
            """
        )

        embedding_success = False
        embedding_status = "PENDING" if parsed_result.get("status") == "SUCCESS" else "SKIPPED"
        statements.append((
            insert_sql,
            (
                parsed_result["ulid"],
                parsed_result["normalized_url"],
                parsed_result["url"],
                parsed_result["title"],
                parsed_result["conclusion"],
                parsed_result["summary"],
                meta_str,
                embedding_status,
                parsed_result["id"],
            ),
        ))

        # Embedding generation (time-bounded)
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

                statements.extend([
                    ("DELETE FROM scraped_articles_vec WHERE rowid = ?", (parsed_result["id"],)),
                    ("INSERT INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)", (parsed_result["id"], embedding_bytes)),
                    ("UPDATE scraped_articles SET embedding_status = 'EMBEDDED' WHERE id = ?", (parsed_result["ulid"],)),
                ])

                # Update local metadata to mark embedding synced
                lm = json.loads(local_meta_json)
                lm["embedding_synced"] = True
                local_meta_json = json.dumps(lm)
                embedding_success = True

            except EmbeddingError as ee:
                log.dual_log(tag="Scraper:Embedding", message=f"Embedding generation failed for {parsed_result['ulid']}: {ee}", level="WARNING", payload={"error_type": type(ee).__name__, "error": str(ee)})
            except TimeoutError as te:
                log.dual_log(tag="Scraper:Embedding", message=f"Snowflake timeout for {parsed_result['ulid']}. Marking pending.", level="WARNING", payload={"error": str(te)})
            except Exception as e:
                log.dual_log(tag="Scraper:Embedding", message=f"Unexpected embedding failure: {e}", level="WARNING", payload={"error_type": type(e).__name__, "error": str(e)})

        # Job item status update (atomic)
        if job_id:
            statements.append((
                "UPDATE job_items SET status = ?, output_data = ?, item_metadata = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE job_id = ? "
                "AND json_extract(item_metadata, '$.step') = json_extract(?, '$.step') "
                "AND json_extract(item_metadata, '$.ulid') = json_extract(?, '$.ulid')",
                ("COMPLETED", local_meta_json, meta_str, job_id, meta_str, meta_str),
            ))

        # Enqueue transaction with tracking
        return enqueue_transaction(statements, track=True), embedding_success

    except Exception as e:
        log.dual_log(tag="Scraper:Persist", message=f"Atomic persist failed: {e}", level="ERROR", payload={"error": str(e)})
        return None, False




def _curate_articles_dfeed(results: dict[str, dict]) -> dict:
    """Trim article pool to 80% of context budget; return curation payload."""
    articles = [
        r for r in results.values()
        if r.get("status") == "SUCCESS" and r.get("title") and r.get("conclusion")
    ]

    if not articles:
        return {"status": "NO_CONTENT", "articles": []}

    sorted_articles = sorted(
        articles,
        key=lambda x: len(str(x.get("conclusion", ""))),
        reverse=True,
    )

    budget_80 = int(getattr(config, "LLM_CONTEXT_CHAR_LIMIT", 40_000) * 0.8)
    curated: list[dict] = []
    current_len = 0

    for art in sorted_articles:
        entry = {
            "title":      art.get("title", ""),
            "conclusion": art.get("conclusion", ""),
            "url":        art.get("url", ""),
        }
        item_len = len(json.dumps(entry))
        if current_len + item_len <= budget_80:
            curated.append(entry)
            current_len += item_len
        else:
            break

    return {
        "status":         "READY_FOR_CURATION",
        "total_eligible": len(articles),
        "articles":       curated,
    }
