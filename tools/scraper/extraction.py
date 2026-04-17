# tools/scraper/extraction.py
"""URL discovery and per-article processing loops."""

import random
from bs4 import BeautifulSoup
from botasaurus.browser import Driver

from tools.scraper.targets import ARTICLE_BODY_SELECTORS
from tools.scraper.browser import (
    _wait_for_any_selector,
    _capture_screenshot_b64,
    _build_multimodal_messages,
    extract_hybrid_html,
)
from utils.som_utils import inject_som, wait_for_dom_stability
from utils.browser_utils import safe_google_get
from utils.text_processing import clean_html_for_agent, parse_llm_json
from tools.scraper.scraper_prompts import VALIDATION_PROMPT, SUMMARIZATION_PROMPT
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


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
        _wait_for_any_selector(driver, target["selectors"], timeout=15)
        driver.scroll_to_bottom()
        driver.short_random_sleep()

        try:
            wait_for_dom_stability(driver)
            last_id = inject_som(driver, start_id=1)
            if last_id > 1:
                target_id = random.randint(1, last_id - 1)
                log.dual_log(
                    tag="Scraper:Engagement",
                    message=f"Engagement scroll to data-ai-id {target_id}",
                    level="INFO",
                    payload={"ai_id": target_id},
                )
                element = driver.select(f'[data-ai-id="{target_id}"]')
                if element:
                    element.scroll_into_view()
        except Exception as e:
            log.dual_log(tag="Scraper:Engagement", message=f"Engagement scroll failed: {e}", level="DEBUG")

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
            message=f"Link extraction failed for {target['name']}: {exc}",
            level="ERROR",
            exc_info=exc,
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
    """Validation loop (3 retries) then summarisation loop (3 retries).

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
            update_item_status(job_id, norm_url, status, _json.dumps(local_meta))

    # Phase 2: Both prior sub-steps confirmed; signal caller to handle embed-only path.
    # This guard is a safety net; task.py's pre-loop check normally prevents reaching here.
    if local_meta.get("validation_passed") and local_meta.get("summary_generated"):
        return {"status": "RESUME_EMBED_ONLY"}
    for val_attempt in range(1, 4):
        try:
            # ── Load page ──────────────────────────────────────────────────
            log.dual_log(
                tag="Scraper:Navigate",
                message=f"Navigating to article: {url}",
                level="INFO",
                payload={"url": url},
            )
            safe_google_get(driver, url)
            _wait_for_any_selector(driver, ARTICLE_BODY_SELECTORS, timeout=15)
            driver.scroll_to_bottom()
            driver.short_random_sleep()

            try:
                wait_for_dom_stability(driver)
                last_id = inject_som(driver, start_id=1)
                if last_id > 1:
                    target_id = random.randint(1, last_id - 1)
                    element = driver.select(f'[data-ai-id="{target_id}"]')
                    if element:
                        element.scroll_into_view()
            except Exception as e:
                log.dual_log(tag="Scraper:Engagement", message=f"Engagement scroll failed: {e}", level="DEBUG")

            driver.short_random_sleep()

            for _body_sel in ARTICLE_BODY_SELECTORS:
                if driver.is_element_present(_body_sel):
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
                slim_val  = clean_html_for_agent(raw_html, max_chars=15_000)
                val_msgs  = _build_multimodal_messages(
                    VALIDATION_PROMPT, 
                    escape_prompt_separators(slim_val) + "\n###", 
                    b64_image
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
                        message=f"Page invalid ({reason}). Scraping failed.",
                        level="WARNING",
                    )
                    # Agentic recovery removed per Exorcism (PLAN-03): do not invoke deprecated agentic components.
                    # If validation fails, fail fast. Preserve cancellation semantics.
                    if cancellation_flag is not None and cancellation_flag.is_set():
                        return {"status": "CANCELED", "reason": "User requested stop via Human Help mode."}
                    return {
                        "status": "FAILED",
                        "reason": (
                            "❌ Scraping failed: Unresolvable blocker encountered. Agentic recovery disabled."
                        ),
                    }
                # Phase 2: Local state update first, then fire-and-forget DB persist.
                local_meta["validation_passed"] = True
                _sync_meta("RUNNING")

            # ── Summarisation (only on valid page) ─────────────────────────
            # slim_sum is computed once and refreshed only after re-navigation.
            slim_sum = clean_html_for_agent(raw_html, max_chars=40_000)

            for sum_attempt in range(1, 4):
                from utils.text_processing import escape_prompt_separators
                sum_msgs = _build_multimodal_messages(
                    SUMMARIZATION_PROMPT, 
                    escape_prompt_separators(slim_sum) + "\n###", 
                    b64_image
                )
                sum_resp = sync_llm_chat(sum_msgs)
                # Phase 2: Boundary log — raw LLM summarization response.
                log.dual_log(
                    tag="Scraper:Summarize:Response",
                    message="LLM summarization response received",
                    payload={
                        "url": url,
                        "raw_content": sum_resp.content,
                        "raw_len": len(sum_resp.content or ""),
                        "sum_attempt": sum_attempt,
                    },
                )
                content  = (sum_resp.content or "").strip()

                if content and content != "INSUFFICIENT_CONTENT" and len(content) > 50:
                    log.dual_log(
                        tag="Scraper:Summarize",
                        message=f"Generated summary for {url}",
                        payload={"url": url, "summary": content},
                    )
                    # Phase 2: Local state update first, then fire-and-forget DB persist.
                    local_meta["summary_generated"] = True
                    _sync_meta("RUNNING")
                    return {"status": "SUCCESS", "summary": content}

                log.dual_log(
                    tag="Scraper:Summarize",
                    message=f"Insufficient summary. Attempt {sum_attempt}/3.",
                    level="WARNING",
                )

                # Re-navigate on attempts 1 and 2; attempt 3 falls through to FAILED.
                if sum_attempt < 3:
                    log.dual_log(
                        tag="Scraper:Navigate",
                        message=f"Re-navigating for summarisation retry: {url}",
                        level="INFO",
                        payload={"url": url, "sum_attempt": sum_attempt},
                    )
                    safe_google_get(driver, url)
                    _wait_for_any_selector(driver, ARTICLE_BODY_SELECTORS, timeout=15)
                    wait_for_dom_stability(driver)

                    for _body_sel in ARTICLE_BODY_SELECTORS:
                        if driver.is_element_present(_body_sel):
                            element = driver.select(_body_sel)
                            if element:
                                element.scroll_into_view()
                            break

                    # CRITICAL: refresh all extraction inputs so the next attempt
                    # builds sum_msgs from the newly captured page state.
                    raw_html, html_len = extract_hybrid_html(driver)
                    b64_image = _capture_screenshot_b64(driver)
                    slim_sum  = clean_html_for_agent(raw_html, max_chars=40_000)

            return {
                "status": "FAILED",
                "reason": "Summarisation returned empty or insufficient content after 3 attempts",
            }

        except Exception as exc:
            log.dual_log(
                tag="Scraper:Process",
                message=f"Error processing {url} on attempt {val_attempt}: {exc}",
                level="ERROR",
                exc_info=exc,
            )

    return {"status": "FAILED", "reason": "Validation failed after 3 attempts"}
