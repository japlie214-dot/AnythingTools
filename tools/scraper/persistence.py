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
    _id = int(ulid_str[:8], 36) % (2 ** 63)

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


def _persist_scraped_article(parsed_result: dict) -> None:
    """Persist article metadata and embedding via paired fire-and-forget writes."""
    # Lazy imports: avoid connection-pool initialisation at module load time.
    from database.writer import enqueue_write
    try:
        enqueue_write(
            """
            INSERT INTO scraped_articles (
                id, normalized_url, url, title, conclusion, summary,
                metadata_json, embedding_status, vec_rowid, scraped_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(normalized_url) DO UPDATE SET
                url = excluded.url,
                title = excluded.title,
                conclusion = excluded.conclusion,
                summary = excluded.summary,
                embedding_status = excluded.embedding_status,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                parsed_result["ulid"],
                parsed_result["normalized_url"],
                parsed_result["url"],
                parsed_result["title"],
                parsed_result["conclusion"],
                parsed_result["summary"],
                "PENDING" if parsed_result["status"] == "SUCCESS" else "SKIPPED",
                parsed_result["id"],
            ),
        )

        if parsed_result["status"] == "SUCCESS":
            from clients.snowflake_client import snowflake_client  # lazy: heavy singleton
            _t = parsed_result["title"]
            _c = parsed_result["conclusion"]
            _s = parsed_result.get("summary", "")
            _header = f"{_t}: {_c}: "
            _avail = 8000 - len(_header)
            if _avail <= 0:
                log.dual_log(
                    tag="Scraper:Embedding",
                    message="Title+Conclusion exceed 8000-char embedding budget; Summary omitted.",
                    level="WARNING",
                    payload={"ulid": parsed_result["ulid"], "header_len": len(_header)},
                )
            content_for_embedding = _header + (_s[:_avail] if _avail > 0 else "")
            try:
                from utils.vector_search import generate_embedding_sync
                embedding_bytes = generate_embedding_sync(content_for_embedding)

                enqueue_write(
                    "INSERT INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)",
                    (parsed_result["id"], embedding_bytes),
                )
                enqueue_write(
                    "UPDATE scraped_articles SET embedding_status = 'EMBEDDED' WHERE id = ?",
                    (parsed_result["ulid"],),
                )
            except Exception as e:
                log.dual_log(
                    tag="Scraper:Embedding",
                    message=f"Failed to generate embedding for article {parsed_result['ulid']}: {e}",
                    level="WARNING",
                    exc_info=e,
                )

        log.dual_log(
            tag="Scraper:Persist",
            message="Persisted article to database",
            payload={"id": parsed_result["ulid"], "url": parsed_result["url"],
                     "status": parsed_result["status"]},
        )

    except Exception as e:
        log.dual_log(
            tag="Scraper:Persist",
            message=f"Failed to persist article: {e}",
            level="ERROR",
            exc_info=e,
        )


def _curate_articles_drip_feed(results: dict[str, dict]) -> dict:
    """Trim article pool to 80 % of context budget; return curation payload."""
    # Both title AND conclusion must be present; SUCCESS_NO_PARSE entries are excluded.
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
            break  # remaining articles exceed budget; stop packing

    return {
        "status":         "READY_FOR_CURATION",
        "total_eligible": len(articles),
        "articles":       curated,
    }
