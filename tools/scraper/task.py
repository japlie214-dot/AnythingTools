# tools/scraper/task.py
"""Botasaurus @browser entry-point that orchestrates one scraping session."""

import os
from botasaurus.browser import Driver
import json

import config
from tools.scraper.targets import TARGETS
from tools.scraper.extraction import extract_links, process_article
from tools.scraper.persistence import (
    _parse_article_result,
    _persist_scraped_article,
)
from database.connection import DatabaseManager
from utils.text_processing import normalize_url
from utils.metadata_helpers import make_metadata
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


def _run_botasaurus_scraper_inner(driver: Driver, data: dict) -> dict:
    """Process a single target site: extract links, deduplicate, process articles."""
    sync_telemetry    = data["sync_telemetry"]
    sync_llm_chat     = data["sync_llm_chat"]
    cancellation_flag = data.get("cancellation_flag")
    target_site       = data.get("target_site")

    target = next((t for t in TARGETS if t["name"] == target_site), None)
    if not target:
        return {"ERROR": {"status": "FAILED",
                          "reason": f"Target site '{target_site}' not found in TARGETS."}}

    if cancellation_flag is not None and cancellation_flag.is_set():
        return {}

    sync_telemetry(f"Extracting links from {target['name']}...")
    links = extract_links(driver, target)

    # ── Deduplication ──────────────────────────────────────────────────────
    normalized_to_raw: dict[str, str] = {normalize_url(link): link for link in links}
    try:
        conn         = DatabaseManager.get_read_connection()
        placeholders = ",".join("?" for _ in normalized_to_raw)
        # DISTINCT preserves original query semantics.
        existing = {
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT normalized_url FROM scraped_articles "
                f"WHERE normalized_url IN ({placeholders})",
                list(normalized_to_raw.keys()),
            ).fetchall()
        }
        deduped_urls = [normalized_to_raw[n] for n in normalized_to_raw if n not in existing]
    except Exception as e:
        log.dual_log(
            tag="Scraper:Dedup",
            message=f"Database check failed, proceeding with all links: {e}",
            level="WARNING",
        )
        deduped_urls = links

    # Phase 2: Pre-flight Job Item Generation (idempotent). Create job_items rows for every
    # deduped URL so the relational state machine has authoritative entries before processing.
    job_id = data.get("job_id")
    if job_id:
        from database.job_queue import add_job_item as _add_ji
        _init_meta = json.dumps({
            "validation_passed": False, "summary_generated": False,
            "embedding_synced": False, "retryable": False,
        })
        for _lnk in deduped_urls:
            _norm = normalize_url(_lnk)
            _meta = make_metadata("scrape", _norm)
            _add_ji(job_id, _meta, _init_meta)

    # Black Box boundary log (Rule 4 — layer-crossing data object).
    log.dual_log(
        tag="Scraper:Links",
        message=f"Discovered article links for {target['name']}",
        payload={
            "target":          target["name"],
            "links":           links,
            "deduped_count":   len(deduped_urls),
            "duplicate_count": len(links) - len(deduped_urls),
        },
    )
    sync_telemetry(
        f"Found {len(links)} articles (deduped to {len(deduped_urls)}) on {target['name']}."
    )

    # ── Article processing loop ────────────────────────────────────────────
    results: dict[str, dict] = {}

    from database.job_queue import update_item_status as _upd
    _read_conn = DatabaseManager.get_read_connection()
    _stats = {
        "new": 0, "resumed_retried": 0,
        "skipped_complete": 0, "skipped_abandoned": 0,
        "success": 0, "fail": 0, "fail_reasons": [],
    }

    for idx, link in enumerate(deduped_urls, 1):
        _norm = normalize_url(link)
        _local_meta = {
            "validation_passed": False, "summary_generated": False,
            "embedding_synced": False, "retryable": False,
        }
        _item_status = "PENDING"
        _is_resume = False

        if cancellation_flag is not None and cancellation_flag.is_set():
            results[link] = {"status": "CANCELED", "reason": "User requested stop."}
            continue

        # Phase 2: Authoritative resume check.
        # Prefer output_data (latest update_item_status write) over input_data (initial add).
        if job_id:
            _row = _read_conn.execute(
                "SELECT status, output_data, input_data FROM job_items "
                "WHERE job_id = ? "
                "AND json_extract(item_metadata, '$.step') = 'scrape' "
                "AND json_extract(item_metadata, '$.ulid') = ?",
                (job_id, _norm),
            ).fetchone()
            if _row:
                _item_status = _row["status"] or "PENDING"
                _stored = _row["output_data"] or _row["input_data"]
                if _stored:
                    _local_meta.update(json.loads(_stored))
                _is_resume = _item_status != "PENDING"

        # Unified skip condition: COMPLETED or FAILED without retryable flag.
        if _item_status == "COMPLETED":
            _stats["skipped_complete"] += 1
            # Ensure already completed articles are included in batch results for curation.
            _existing_data = _read_conn.execute(
                "SELECT id, normalized_url, title, conclusion FROM scraped_articles WHERE normalized_url = ?",
                (_norm,)
            ).fetchone()
            if _existing_data:
                results[link] = {
                    "status": "SUCCESS", "ulid": _existing_data["id"],
                    "normalized_url": _norm, "title": _existing_data["title"],
                    "conclusion": _existing_data["conclusion"]
                }
            continue
        if _item_status == "FAILED" and not _local_meta.get("retryable", False):
            _stats["skipped_abandoned"] += 1
            continue

        if _is_resume:
            _stats["resumed_retried"] += 1
            sync_telemetry(
                f"[{target['name']}] Resuming article {idx}/{len(deduped_urls)} "
                f"(v={_local_meta['validation_passed']} "
                f"s={_local_meta['summary_generated']} "
                f"e={_local_meta['embedding_synced']})..."
            )
        else:
            _stats["new"] += 1
            sync_telemetry(f"[{target['name']}] Processing article {idx}/{len(deduped_urls)}...")

        log.dual_log(
            tag="Scraper:Progress",
            message=f"[{idx}/{len(deduped_urls)}] Processing article for {target['name']}",
            level="INFO",
            payload={"index": idx, "total": len(deduped_urls), "url": link, "resume": _is_resume},
        )

        if job_id:
            _upd(job_id, _meta, "RUNNING", json.dumps(_local_meta))

        # RESUME_EMBED_ONLY: validation and summarization confirmed from prior run.
        # Read the existing scraped_articles row and regenerate only the embedding.
        if _local_meta["validation_passed"] and _local_meta["summary_generated"]:
            _existing = _read_conn.execute(
                "SELECT id, vec_rowid, title, conclusion, summary "
                "FROM scraped_articles WHERE normalized_url = ?",
                (_norm,),
            ).fetchone()
            if _existing:
                import struct as _struct
                from clients.snowflake_client import snowflake_client as _sf
                from database.writer import enqueue_write as _ew
                _eh = f"{_existing['title'] or ''}: {_existing['conclusion'] or ''}: "
                _ea = 8000 - len(_eh)
                _et = _eh + ((_existing["summary"] or "")[:_ea] if _ea > 0 else "")
                try:
                    _emb = _sf.embed(_et)
                    _eb = _struct.pack(f"{len(_emb)}f", *_emb)
                    _ew(
                        "INSERT OR REPLACE INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)",
                        (_existing["vec_rowid"], _eb),
                    )
                    _ew(
                        "UPDATE scraped_articles SET embedding_status = 'EMBEDDED' WHERE id = ?",
                        (_existing["id"],),
                    )
                    _local_meta["embedding_synced"] = True
                    if job_id:
                        _upd(job_id, _meta, "COMPLETED", json.dumps(_local_meta))
                    _stats["success"] += 1
                    results[link] = {
                        "status": "SUCCESS", "ulid": _existing["id"],
                        "normalized_url": _norm,
                        "title": _existing["title"], "conclusion": _existing["conclusion"],
                    }
                except Exception as _ee:
                    log.dual_log(
                        tag="Scraper:ResumeEmbed",
                        message=f"Re-embed failed for {_norm}: {_ee}",
                        level="ERROR", exc_info=_ee,
                    )
                    _local_meta["retryable"] = True
                    if job_id:
                        _upd(job_id, _meta, "FAILED", json.dumps(_local_meta))
                    _stats["fail"] += 1
                    _stats["fail_reasons"].append(f"{link}: EmbedError")
            continue  # Skip normal process_article path in all RESUME_EMBED_ONLY cases.

        raw_result = process_article(
            driver, link, sync_llm_chat, cancellation_flag,
            local_meta=_local_meta, job_id=job_id, norm_url=_norm,
        )
        parsed_result = _parse_article_result(raw_result, link)
        results[link] = parsed_result

        if parsed_result.get("status") in ("SUCCESS", "SUCCESS_NO_PARSE"):
            _persist_scraped_article(parsed_result)
            _local_meta["embedding_synced"] = True
            if job_id:
                _upd(job_id, _meta, "COMPLETED", json.dumps(_local_meta))
            _stats["success"] += 1
        else:
            _local_meta["retryable"] = True
            if job_id:
                _upd(job_id, _meta, "FAILED", json.dumps(_local_meta))
            _stats["fail"] += 1
            _stats["fail_reasons"].append(
                f"{link}: {parsed_result.get('reason', 'Unknown')}"
            )

    results["_stats"] = _stats
    return results


def _run_botasaurus_scraper(driver: Driver, data: dict) -> dict:
    """Public entry point.  Detects dead-session WebDriver errors and re-raises
    as RuntimeError so ScraperTool's finally block always releases browser_lock,
    allowing get_or_create_driver() to re-initialise cleanly on the next call."""
    try:
        return _run_botasaurus_scraper_inner(driver, data)
    except Exception as _exc:
        _msg = str(_exc).lower()
        _dead = (
            "invalid session id" in _msg
            or "not reachable"   in _msg
            or "no such window"  in _msg
            or "connection refused" in _msg
            or "target closed"   in _msg
        )
        if _dead:
            raise RuntimeError(f"Browser session lost mid-scrape: {_exc}") from _exc
        raise
