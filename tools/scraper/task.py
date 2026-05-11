# tools/scraper/task.py
"""Browser entry-point that orchestrates a scraping session."""

import os
from botasaurus.browser import Driver
import json
import math

import config
from tools.scraper.targets import TARGETS
from tools.scraper.extraction import extract_links
from tools.scraper.article_processor import process_article
from tools.scraper.persistence import (
    _parse_article_result,
    _sync_scraped_article_atomic,  # NEW: Atomic persistence with WriteReceipt
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

    job_id = data.get("job_id")
    _read_conn = DatabaseManager.get_read_connection()
    
    existing_items = []
    if job_id:
        existing_items = _read_conn.execute(
            "SELECT json_extract(item_metadata, '$.ulid') as norm_url "
            "FROM job_items WHERE job_id = ? "
            "AND json_extract(item_metadata, '$.step') = 'scrape'",
            (job_id,)
        ).fetchall()

    if existing_items:
        deduped_urls = [r["norm_url"] for r in existing_items if r["norm_url"]]
        links = deduped_urls
        log.dual_log(
            tag="Scraper:Resume:SkipExtract",
            message=f"Bypassing extract_links. Found {len(deduped_urls)} existing items.",
            payload={"existing_count": len(existing_items)}
        )
    else:
        sync_telemetry(f"Extracting links from {target['name']}...")
        links = extract_links(driver, target)

        # ── Deduplication ──────────────────────────────────────────────────────
        normalized_to_raw: dict[str, str] = {normalize_url(link): link for link in links}
        try:
            conn = DatabaseManager.get_read_connection()
            placeholders = ",".join("?" for _ in normalized_to_raw)
            existing_db = {
                row[0] for row in conn.execute(
                    f"SELECT DISTINCT normalized_url FROM scraped_articles "
                    f"WHERE normalized_url IN ({placeholders})",
                    list(normalized_to_raw.keys()),
                ).fetchall()
            }
            deduped_urls = [normalized_to_raw[n] for n in normalized_to_raw if n not in existing_db]
        except Exception as e:
            log.dual_log(
                tag="Scraper:Dedup",
                message="Database check failed, proceeding with all links",
                level="WARNING",
                payload={"error": str(e)},
            )
            deduped_urls = links

    # Phase 1.: PARTIAL item status - filter to only failed items
    job_id = data.get("job_id")
    if job_id:
        try:
            _read_conn = DatabaseManager.get_read_connection()
            failed_rows = _read_conn.execute(
                "SELECT json_extract(item_metadata, '$.ulid') as norm_url "
                "FROM job_items WHERE job_id = ? AND status = 'FAILED' "
                "AND json_extract(item_metadata, '$.step') = 'scrape'",
                (job_id,)
            ).fetchall()
            failed_urls = {r["norm_url"] for r in failed_rows if r["norm_url"]}
            if failed_urls:
                mapping = locals().get("normalized_to_raw")
                if mapping:
                    deduped_urls = [mapping.get(n, n) for n in failed_urls]
                else:
                    deduped_urls = [n for n in failed_urls]
                log.dual_log(
                    tag="Scraper:Partial",
                    message=f"PARTIAL resumption: restricting to {len(deduped_urls)} failed links.",
                    payload={"failed_count": len(deduped_urls), "total_links": len(links), "job_id": job_id, "target_site": target_site}
                )
        except Exception as e:
            log.dual_log(
                tag="Scraper:Partial",
                message="PARTIAL resumption check failed",
                level="WARNING?",
                payload={"error": str(e)}
            )

    # Phase 2: Pre-flight Job Item Idempotency
    if job_id:
        from database.job_queue import add_job_item as _add_ji
        init_meta = json.dumps({
            "validation_passed": False, "summary_generated": False,
            "embedding_synced": False, "retryable": False,
        })
        for _lnk in deduped_urls:
            _norm = normalize_url(_lnk)
            _meta = make_metadata("scrape", _norm)
            _add_ji(job_id, _meta, init_meta)

    # Black box boundary log
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
    _stats = {
        "new": 0, "resumed_retried": 0,
        "skipped_complete": 0, "skipped_abandoned": 0,
        "skipped_auto": 0, "skipped_auto_current": 0,
        "success": 0, "fail": 0, "fail_reasons": [],
        }

    import math
    max_nav_failures = math.ceil(len(deduped_urls) * 1.2) if deduped_urls else 1
    consecutive_nav_failures = 0

    for idx, link in enumerate(deduped_urls, 1):
        _norm = normalize_url(link)
        _meta = make_metadata("scrape", _norm)                     # JOB IDENTITY META
        _local_meta = {
            "validation_passed": False, "summary_generated": False,
            "embedding_synced": False, "retryable": False,           # DATA FLAGS
        }
        _item_status = "PENDING"
        _is_resume = False

        if cancellation_flag is not None and cancellation_flag.is_set():
            results[link] = {"status": "CANCELED", "reason": "User requested stop."}
            continue

        #   Unified status check
        if job_id:
            _row = _read_conn.execute(
                "SELECT status, output_data FROM job_items WHERE job_id = ? "
                "AND json_extract(item_metadata, '$.step') = 'scrape' "
                "AND json_extract(item_metadata, '$.ulid') = ?",
                (job_id, _norm),
            ).fetchone()
            if _row:
                _item_status = _row["status"] or "PENDING"
                if _row["output_data"]:
                    _local_meta = json.loads(_row["output_data"])
                _is_resume = _item_status != "PENDING"

        # Skip loops for completed / abandoned:
        if _item_status == "COMPLETED":
            _stats["skipped_complete"] += 1
            _existing_data = _read_conn.execute(
                "SELECT id, normalized_url, title, conclusion FROM scraped_articles WHERE normalized_url = ?",
                (_norm,),
            ).fetchone()
            if _existing_data:
                results[link] = {
                    "status": "SUCCESS", "ulid": _existing_data["id"],
                    "normalized_url": _norm, "title": _existing_data["title"],
                    "conclusion": _existing_data["conclusion"],
                }
            continue
        if _item_status == "FAILED" and not _local_meta.get("retryable", False):
            _stats["skipped_abandoned"] += 1
            continue
        if _item_status == "SKIPPED":
            _stats["skipped_auto"] += 1
            continue

        # Unified new/ resume progress info
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
            tag="Scraper:Process",
            message=f"[{idx}/{len(deduped_urls)}] Processing article for {target['name']}",
            level="INFO",
            payload={"index": idx, "total": len(deduped_urls), "link": link, "resume": _is_resume},
        )

        # Update job status to RUNNING
        if job_id:
            _upd(job_id, _meta, "RUNNING", json.dumps(_local_meta))

        # Respect prior partial success break.
        if _local_meta["validation_passed"] and _local_meta["summary_generated"]:
            _existing = _read_conn.execute(
                "SELECT id, vec_rowid, title, conclusion, summary "
                "FROM scraped_articles WHERE normalized_url = ?",
                (_norm,),
            ).fetchone()
            if _existing:
                try:
                    # Only re-generate embedding on failure/resumable                
                    if not _local_meta["embedding_synced"]:
                        _entry = {
                            "conclusion": _existing["conclusion"],
                            "title": _existing["title"],
                            "overview": _existing["summary"]
                        }
                        # Very short sync inject (simplified)
                        from database.writer import enqueue_write
                        _emb_receipt, _embed_ok = _sync_scraped_article_atomic(
                            _entry, job_id, _meta, json.dumps(_local_meta)
                        )
                        if _embed_ok:
                            _local_meta["embedding_synced"] = True
                        elif job_id:
                            enqueue_write("UPDATE jobs SET status = 'PARTIAL', updated_at = CURRENT_TIMESTAMP WHERE job_id = ?", (job_id,))
                            _job_final_status = "PARTIAL"
                    _stats["success"] += 1
                    results[link] = {
                        "status": "SUCCESS", "ulid": _existing["id"],
                        "normalized_url": _norm,
                        "title": _existing["title"], "conclusion": _existing["conclusion"],
                    }
                    # Local meta even permanent
                    continue 
                except Exception as _ee:
                    log.dual_log(tag="Scraper:ResumeEmbed:Error", message=f"Resume embedding failed: {_ee}", level="ERROR", payload={"error": str(_ee)})
            _local_meta["retryable"] = True

        # ── Run `process_article` for new summaries or when validation FAILED previously
        _raw_result = process_article(
            driver, link, sync_llm_chat, cancellation_flag,
            local_meta=_local_meta, job_id=job_id, norm_url=_norm,
        )

        _parsed_result = _parse_article_result(_raw_result, link)
        results[link] = _parsed_result

        # Monitoring Failures
        if "Navigation failed" in _parsed_result.get("reason", ""):
            consecutive_nav_failures += 1
            if consecutive_nav_failures >= max_nav_failures:
                raise RuntimeError(f"Max consecutive navigation failures reached ({consecutive_nav_failures}). Abandoning job.")
        
        elif _parsed_result.get("status") == "SKIPPED":
            consecutive_nav_failures = 0
            _stats["skipped_auto"] += 1
            _stats["skipped_auto_current"] += 1
            sync_telemetry(f"[{target['name']}] Auto-skipped article {idx}/{len(deduped_urls)}: {_parsed_result.get('reason', 'N/A')}")
            if job_id:
                _upd(job_id, _meta, "SKIPPED", json.dumps(_local_meta))
        elif _parsed_result.get("status") in ("SUCCESS", "SUCCESS_NO_PARSE"):
            consecutive_nav_failures = 0
            # ── ATOM IN: THE BIG ONE ──
            from database.writer import enqueue_write
            _receipt, _embed_ok = _sync_scraped_article_atomic(
                _parsed_result, job_id, _meta, json.dumps(_local_meta)
            )
            if not _embed_ok and job_id:
                enqueue_write("UPDATE jobs SET status = 'PARTIAL', updated_at = CURRENT_TIMESTAMP WHERE job_id = ?", (job_id,))
                _job_final_status = "PARTIAL"
            if _receipt:
                if not _receipt.wait(timeout=45.0):
                    raise RuntimeError("Critical database write flush timed out after 45s. Aborting.")
                if _receipt.error:
                    raise _receipt.error
                # If the receipt resolved without error, the atomic transaction succeeded.
                if _embed_ok:
                    _local_meta["embedding_synced"] = True
            
            _stats["success"] += 1
        else:
            _local_meta["retryable"] = True
            if job_id:
                _upd(job_id, _meta, "FAILED", json.dumps(_local_meta))
            _stats["fail"] += 1
            _stats["fail_reasons"].append(
                f"{link}: {_parsed_result.get('reason', 'Unknown')}"
            )

    total_attempted = _stats["new"] + _stats["resumed_retried"]
    
    # Job finalized status
    total_successful_outcomes = _stats["success"] + _stats["skipped_auto_current"]
    all_embedded = (total_successful_outcomes == total_attempted) and (total_attempted > 0)
    job_final_status = "COMPLETED"
    if _stats["fail"] > 0 or (total_attempted > 0 and not all_embedded) or '_job_final_status' in locals() and _job_final_status == "PARTIAL":
        job_final_status = "PARTIAL"

    log.dual_log(
        tag="Scraper:Job:Status",
        message=f"Job finalization status: {job_final_status}",
        payload={
            "final_status": job_final_status,
            "total_attempted": total_attempted,
            "success": _stats["success"],
            "fail": _stats["fail"],
        },
    )

    results["_stats"] = _stats
    results["_job_final_status"] = job_final_status
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
