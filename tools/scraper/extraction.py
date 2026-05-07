# tools/scraper/extraction.py
"""URL discovery and per-article processing loops."""

import random
from bs4 import BeautifulSoup
from botasaurus.browser import Driver

from tools.scraper.targets import ARTICLE_BODY_SELECTORS
from tools.scraper.browser import (
    _safe_wait_for_any_selector,
    _capture_screenshot_b64,
    _build_multimodal_messages,
    extract_hybrid_html,
)
from utils.som_utils import inject_som, wait_for_dom_stability
from utils.browser_utils import safe_google_get
from utils.text_processing import clean_html_for_agent, parse_llm_json
from tools.scraper.scraper_prompts import VALIDATION_PROMPT, SUMMARIZATION_PROMPT
from utils.logger import get_dual_logger
from utils.metadata_helpers import make_metadata
import threading

log = get_dual_logger(__name__)


class HITLState:
    def __init__(self):
        self.pending_url = None
        self.pending_reason = None
        self.lock = threading.Lock()
        self.decision = threading.Event()
        self.decision_result = None

    def request_decision(self, job_id: str | None, url: str, reason: str) -> str:
        """
        Block the current worker thread and wait for operator input.

        IMPORTANT ARCHITECTURE NOTE:
        This function intentionally performs a synchronous blocking `input()` call
        while holding no external locks beyond its internal mutex. The design
        decision to use blocking stdin is intentional: this server runs on a
        local Windows machine with a single operator. Blocking the console is
        the simplest, most reliable way to draw immediate attention to a
        human-in-the-loop intervention without introducing complex async
        coordination or race-prone background threads.

        The database status is synchronously updated to PAUSED_FOR_HITL just
        before blocking so external APIs (like /resume) will observe the
        job as blocked and reject resume attempts with 409 Conflict. After the
        operator responds, the job status is reverted to RUNNING (or left as
        CANCELLING if the operator requested cancellation).
        """
        with self.lock:
            self.pending_url = url
            self.pending_reason = reason
            self.decision.clear()
            self.decision_result = None

        # Synchronously write DB status to PAUSED_FOR_HITL to lock out concurrent resumes.
        if job_id:
            from database.writer import enqueue_write, wait_for_writes
            from datetime import datetime, timezone
            import asyncio
            enqueue_write(
                "UPDATE jobs SET status = 'PAUSED_FOR_HITL', updated_at = ? WHERE job_id = ?",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
            # Ensure the writer flushes so external readers see the PAUSED state.
            try:
                loop = asyncio.get_running_loop()
                asyncio.run_coroutine_threadsafe(wait_for_writes(timeout=5.0), loop).result()
            except RuntimeError:
                # If we're not in an event loop, run directly.
                asyncio.run(wait_for_writes(timeout=5.0))

        log.dual_log(
            tag="Scraper:HITL:Request",
            message=f"Validation failed - awaiting human input",
            level="WARNING",
            payload={"url": url, "reason": reason, "job_id": job_id},
        )

        print(f"\n\n[!!!] HITL VALIDATION ALERT")
        print(f">>> URL: {url}")
        print(f">>> Reason: {reason}")
        print(">>> Type 'ENTER' to force PROCEED, 'SKIP' to skip URL, or 'CANCEL' to abort job.")

        # ARCHITECTURAL DECISION: Use blocking stdin. See docstring above.
        try:
            user_input = input("Decision: ").strip().upper()
        except EOFError:
            user_input = "CANCEL"

        # Synchronously revert job status -> RUNNING unless operator cancelled.
        if job_id:
            from database.writer import enqueue_write
            from datetime import datetime, timezone
            if user_input == "CANCEL":
                # Mark cancellation; worker will observe cancellation flag
                enqueue_write(
                    "UPDATE jobs SET status = 'CANCELLING', updated_at = ? WHERE job_id = ?",
                    (datetime.now(timezone.utc).isoformat(), job_id),
                )
            else:
                enqueue_write(
                    "UPDATE jobs SET status = 'RUNNING', updated_at = ? WHERE job_id = ?",
                    (datetime.now(timezone.utc).isoformat(), job_id),
                )

        with self.lock:
            if user_input == "SKIP":
                self.decision_result = "skip"
            elif user_input == "CANCEL":
                self.decision_result = "cancel"
            else:
                self.decision_result = "proceed"
            self.pending_url = None
            self.pending_reason = None

        return self.decision_result


_hitl_state = HITLState()


def extract_links(driver: Driver, target: dict) -> list[str]:
    """Navigate to a site front page and return deduplicated article URLs."""
    try:
        log.dual_log(
            tag="Scraper:Navigate",
            message=f"Navigating to extract links: {target['url']}",
            level="INFO",
            payload={"url": target["url"]},
        )
        safe_google_get(driver, target["url"])
        driver.sleep(45)  # Wait for Quiet
        
        _safe_wait_for_any_selector(driver, target["selectors"], timeout=15)
        wait_for_dom_stability(driver)
        driver.scroll_to_bottom()
        driver.short_random_sleep()
        driver.short_random_sleep()

        try:
            wait_for_dom_stability(driver)
            try:
                from utils.observation_adapter import MarkingError
                last_id = inject_som(driver, start_id=1)
            except MarkingError:
                from utils.browser_daemon import daemon_manager
                daemon_manager.surgical_kill()
                raise RuntimeError("SoM Injection hung. Browser killed.")

            if last_id > 1:
                target_id = random.randint(0, last_id - 2)
                log.dual_log(
                    tag="Scraper:Engagement",
                    message=f"Engagement scroll to data-ai-id bid_{target_id}",
                    level="INFO",
                    payload={"ai_id": f"bid_{target_id}"},
                )
                element = driver.select(f'[data-ai-id="bid_{target_id}"]')
                if element:
                    element.scroll_into_view()
        except Exception as e:
            log.dual_log(tag="Scraper:Engagement", message="Engagement scroll failed", level="DEBUG", payload={"error": str(e)})

        driver.long_random_sleep()

        soup = BeautifulSoup(driver.page_html or "", "html.parser")
        links: set[str] = set()
        base_url = target["url"].rstrip("/")

        # Explicit loop preserves original readability and None-href safety.
        for sel in target["selectors"]:
            for anchor in soup.select(sel):
                href = anchor.get("href")
                if not href:
                    continue
                if href.startswith("/"):
                    href = base_url + href
                if not target["filter"] or target["filter"] in href:
                    links.add(href)

        return list(links)

    except Exception as exc:
        log.dual_log(
            tag="Scraper:Extract",
            message="Link extraction failed",
            level="ERROR",
            exc_info=exc,
            payload={"target": target['name'], "error": str(exc)},
        )
        return []


def process_article(
    driver: Driver,
    url: str,
    sync_llm_chat,
    cancellation_flag=None,
    local_meta: dict | None = None,
    job_id: str | None = None,
    norm_url: str | None = None,
) -> dict:
    """Validation loop then summarisation loop.

    Optional local_meta enables flag-conditional sub-step skipping on resume.
    local_meta is mutated in place by _sync_meta; callers must preserve the
    reference across the call and not discard it.
    """
    if local_meta is None:
        local_meta = {}

    def _sync_meta(status: str) -> None:
        """Update local_meta in the DB via enqueue_write (fire-and-forget).
        Always update local_meta before calling _sync_meta, never after.
        """
        if job_id and norm_url:
            import json as _json
            from database.job_queue import update_item_status
            _meta = make_metadata("scrape", norm_url)
            update_item_status(job_id, _meta, status, _json.dumps(local_meta))

    # Phase 2: Both prior sub-steps confirmed; signal caller to handle embed-only path.
    if local_meta.get("validation_passed") and local_meta.get("summary_generated"):
        return {"status": "RESUME_EMBED_ONLY"}

    # Single attempt: no refresh retries on validation failure
    for val_attempt in range(1, 2):
        try:
            # ── Load page ──────────────────────────────────────────────────
            log.dual_log(
                tag="Scraper:Navigate",
                message=f"Navigating to article: {url}",
                level="INFO",
                payload={"url": url},
            )
            safe_google_get(driver, url)
            driver.sleep(45)  # Wait for Quiet
            
            _safe_wait_for_any_selector(driver, ARTICLE_BODY_SELECTORS, timeout=15)
            wait_for_dom_stability(driver)
            driver.scroll_to_bottom()
            driver.short_random_sleep()
            driver.short_random_sleep()

            try:
                wait_for_dom_stability(driver)
                last_id = inject_som(driver, start_id=1)
                if last_id > 1:
                    target_id = random.randint(0, last_id - 2)
                    log.dual_log(
                        tag="Scraper:Engagement",
                        message=f"Engagement scroll to data-ai-id bid_{target_id}",
                        level="INFO",
                        payload={"ai_id": f"bid_{target_id}"},
                    )
                    element = driver.select(f'[data-ai-id="bid_{target_id}"]')
                    if element:
                        element.scroll_into_view()
            except Exception as e:
                log.dual_log(tag="Scraper:Engagement", message="Engagement scroll failed", level="DEBUG", payload={"error": str(e)})

            driver.short_random_sleep()

            for _body_sel in ARTICLE_BODY_SELECTORS:
                if driver.is_element_present(_body_sel, wait=2):
                    element = driver.select(_body_sel)
                    if element:
                        element.scroll_into_view()
                    break

            raw_html, html_len = extract_hybrid_html(driver)
            b64_image = _capture_screenshot_b64(driver)

            # Phase 2: Boundary log — HTML extraction metrics.
            log.dual_log(
                tag="Scraper:HTML",
                message="Raw HTML captured",
                payload={
                    "url": url,
                    "html_len": len(raw_html),
                    "has_screenshot": b64_image is not None,
                    "val_attempt": val_attempt,
                },
            )

            # ── Pre-Check: Video/Audio Detection ─────────────────────────────
            if not local_meta.get("validation_passed"):
                soup_pre = BeautifulSoup(raw_html or "", "html.parser")
                video_audio_tags = soup_pre.find_all(["video", "audio"])
                iframe_embeds = soup_pre.find_all("iframe", src=True)
                video_platforms = ["youtube.com/embed", "youtu.be", "vimeo.com", "dailymotion.com"]
                has_video_embed = any(
                    any(platform in (tag.get("src") or "") for platform in video_platforms)
                    for tag in iframe_embeds
                )
                if video_audio_tags or has_video_embed:
                    paragraph_text = " ".join(p.get_text(strip=True) for p in soup_pre.find_all("p"))
                    if len(paragraph_text) < 500:
                        log.dual_log(tag="Scraper:Validation", message="Page rejected: video/audio primary content", level="WARNING", payload={"url": url})
                        return {"status": "FAILED", "reason": "Page primary content is video/audio with insufficient article text."}

            # ── Paywall Detection ───────────────────────────────────────────
            if not local_meta.get("validation_passed"):
                from tools.scraper.paywall import PaywallDetector
                pw_result = PaywallDetector().detect(raw_html)
                if pw_result.is_paywalled:
                    log.dual_log(tag="Scraper:Paywall", message="Paywall detected", level="WARNING", payload={"url": url, "attempt": val_attempt})
                    decision = _hitl_state.request_decision(job_id, url, f"Paywall detected. Indicators: {pw_result.detected_indicators}")
                    if decision == "proceed":
                        log.dual_log(tag="Scraper:HITL:Proceed", message="User forced paywall proceed. Re-extracting state.", level="INFO", payload={"url": url})
                        # Do NOT re-navigate. Read the live DOM state the operator has finished setting up.
                        wait_for_dom_stability(driver)
                        raw_html, html_len = extract_hybrid_html(driver)
                        b64_image = _capture_screenshot_b64(driver)
                        slim_sum = raw_html
                        local_meta["validation_passed"] = True
                        _sync_meta("RUNNING")
                    elif decision == "skip":
                        return {"status": "SKIPPED", "reason": f"User skipped after HITL: Paywall detected"}
                    elif decision == "cancel":
                        if cancellation_flag is not None:
                            cancellation_flag.set()
                        return {"status": "CANCELED", "reason": "User requested stop via HITL."}

            # ── Validation ─────────────────────────────────────────────────
            if local_meta.get("validation_passed"):
                # Phase 2: LLM validation call skipped; flag confirmed from prior run.
                log.dual_log(
                    tag="Scraper:Validation",
                    message="Validation skipped (validation_passed=True from prior run).",
                    payload={"url": url},
                )
            else:
                from utils.text_processing import escape_prompt_separators
                slim_val  = raw_html[:15000]
                val_msgs  = _build_multimodal_messages(
                    VALIDATION_PROMPT,
                    escape_prompt_separators(slim_val) + "\n###",
                    b64_image,
                )
                val_resp  = sync_llm_chat(val_msgs, response_format={"type": "json_object"})
                val_data  = parse_llm_json(val_resp.content or "{}")
                # Phase 2: Boundary log — raw LLM validation response.
                log.dual_log(
                    tag="Scraper:Validation:Response",
                    message="LLM validation response received",
                    payload={"url": url, "raw": val_resp.content, "parsed": val_data},
                )
                if not val_data.get("valid", False):
                    reason = val_data.get("reason", "unknown")
                    log.dual_log(
                        tag="Scraper:Validation",
                        message="Page invalid",
                        level="WARNING",
                        payload={"reason": reason, "attempt": val_attempt, "url": url},
                    )
                    if cancellation_flag is not None and cancellation_flag.is_set():
                        return {"status": "CANCELED", "reason": "User requested stop via Human Help mode."}

                    decision = _hitl_state.request_decision(job_id, url, reason)
                    if decision == "proceed":
                        log.dual_log(tag="Scraper:HITL:Proceed", message="User forced validation proceed. Re-extracting state.", level="INFO", payload={"url": url})
                        # Do NOT re-navigate. Read the live DOM state the operator has finished setting up.
                        wait_for_dom_stability(driver)
                        raw_html, html_len = extract_hybrid_html(driver)
                        b64_image = _capture_screenshot_b64(driver)
                        slim_sum = raw_html
                        local_meta["validation_passed"] = True
                        _sync_meta("RUNNING")
                    elif decision == "skip":
                        return {"status": "SKIPPED", "reason": f"User skipped after HITL: {reason}"}
                    elif decision == "cancel":
                        if cancellation_flag is not None:
                            cancellation_flag.set()
                        return {"status": "CANCELED", "reason": "User requested stop via HITL."}

                # Phase 2: Local state update first, then fire-and-forget DB persist.
                local_meta["validation_passed"] = True
                _sync_meta("RUNNING")

            # ── Summarisation (only on valid page) ─────────────────────────
            slim_sum = raw_html

            from tools.scraper.scraper_prompts import SUMMARIZATION_SCHEMA
            for sum_attempt in range(1, 4):
                from utils.text_processing import escape_prompt_separators

                sum_msgs = _build_multimodal_messages(
                    SUMMARIZATION_PROMPT,
                    escape_prompt_separators(slim_sum) + "\n###",
                    b64_image,
                )

                try:
                    sum_resp = sync_llm_chat(
                        sum_msgs,
                        response_format={
                            "type": "json_schema",
                            "json_schema": {"name": "summary", "strict": True, "schema": SUMMARIZATION_SCHEMA},
                        },
                    )
                    used_format = "json_schema"
                except Exception as format_exc:
                    from openai import BadRequestError
                    from clients.llm.utils import is_context_length_error

                    if is_context_length_error(format_exc):
                        log.dual_log(
                            tag="Scraper:Summarize:ContextLength",
                            message="Context length exceeded during json_schema call",
                            level="WARNING",
                            payload={"error": str(format_exc)},
                        )
                        raise

                    if isinstance(format_exc, BadRequestError):
                        log.dual_log(
                            tag="Scraper:Summarize:Fallback",
                            message="json_schema format rejected; falling back to json_object",
                            level="WARNING",
                            payload={"error": str(format_exc)},
                        )
                        sum_resp = sync_llm_chat(sum_msgs, response_format={"type": "json_object"})
                        used_format = "json_object"
                    else:
                        raise

                sum_data = parse_llm_json(sum_resp.content or "{}")

                # Phase 2: Boundary log — raw LLM summarization response.
                log.dual_log(
                    tag="Scraper:Summarize:Response",
                    message=f"LLM summarization response received via {used_format}",
                    payload={
                        "url": url,
                        "raw_content": sum_resp.content,
                        "raw_len": len(sum_resp.content or ""),
                        "sum_attempt": sum_attempt,
                        "parsed_keys": list(sum_data.keys()),
                    },
                )

                if sum_data.get("error") == "INSUFFICIENT_CONTENT":
                    log.dual_log(
                        tag="Scraper:Summarize",
                        message="Insufficient content",
                        level="WARNING",
                        payload={"attempt": sum_attempt, "url": url},
                    )
                elif sum_data.get("title") and sum_data.get("conclusion"):
                    log.dual_log(
                        tag="Scraper:Summarize",
                        message=f"Generated structured summary for {url}",
                        payload={"url": url, "title": sum_data.get("title", "")[:50]},
                    )
                    local_meta["summary_generated"] = True
                    _sync_meta("RUNNING")
                    return {"status": "SUCCESS", "parsed_json": sum_data}
                else:
                    log.dual_log(
                        tag="Scraper:Summarize",
                        message="Missing mandatory fields in JSON",
                        level="WARNING",
                        payload={"parsed": sum_data, "attempt": sum_attempt, "url": url},
                    )

                # Re-navigate on attempts 1 and 2; attempt 3 falls through to FAILED.
                if sum_attempt < 3:
                    log.dual_log(
                        tag="Scraper:Navigate",
                        message="Re-navigating for summarisation retry",
                        level="INFO",
                        payload={"url": url, "sum_attempt": sum_attempt},
                    )
                    safe_google_get(driver, url)
                    driver.sleep(45)  # Wait for Quiet
                    _safe_wait_for_any_selector(driver, ARTICLE_BODY_SELECTORS, timeout=15)
                    wait_for_dom_stability(driver)
                    
                    for _body_sel in ARTICLE_BODY_SELECTORS:
                        if driver.is_element_present(_body_sel, wait=2):
                            element = driver.select(_body_sel)
                            if element:
                                element.scroll_into_view()
                            break
                    raw_html, html_len = extract_hybrid_html(driver)
                    b64_image = _capture_screenshot_b64(driver)
                    slim_sum  = raw_html

            return {
                "status": "FAILED",
                "reason": "Summarisation returned empty or insufficient content after 3 attempts",
            }

        except Exception as exc:
            log.dual_log(
                tag="Scraper:Process",
                message="Error processing article",
                level="ERROR",
                exc_info=exc,
                payload={"url": url, "attempt": val_attempt, "error": str(exc)},
            )

    return {"status": "FAILED", "reason": "Validation failed after 1 attempt"}
