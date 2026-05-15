# tools/scraper/article_processor.py
"""Per-article processing orchestrator."""

import random
import json
from tools.scraper.browser import (
    _safe_wait_for_any_selector,
    _capture_screenshot_b64,
    extract_hybrid_html,
)
from tools.scraper.targets import ARTICLE_BODY_SELECTORS
from tools.scraper.validation import check_video_audio_skip, check_paywall, validate_article
from tools.scraper.summarization import summarize_article
from tools.scraper.hitl import ValidationAction, _hitl_state
from utils.som_utils import inject_som, wait_for_dom_stability
from utils.browser_utils import safe_google_get
from utils.metadata_helpers import make_metadata
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def process_article(
    driver,
    url: str,
    sync_llm_chat,
    cancellation_flag=None,
    local_meta: dict | None = None,
    job_id: str | None = None,
    norm_url: str | None = None,
) -> dict:
    if local_meta is None:
        local_meta = {}

    def _sync_meta(status: str) -> None:
        if job_id and norm_url:
            from database.job_queue import update_item_status
            _meta = make_metadata("scrape", norm_url)
            update_item_status(job_id, _meta, status, json.dumps(local_meta))

    if local_meta.get("validation_passed") and local_meta.get("summary_generated"):
        return {"status": "RESUME_EMBED_ONLY"}

    for val_attempt in range(1, 2):
        try:
            log.dual_log(tag="Scraper:Navigation:Request", message=f"Navigating to article: {url}", level="INFO", payload={"url": url})
            safe_google_get(driver, url)
            
            _safe_wait_for_any_selector(driver, ARTICLE_BODY_SELECTORS, timeout=15)
            wait_for_dom_stability(driver)
            driver.scroll_to_bottom()
            driver.short_random_sleep()

            try:
                wait_for_dom_stability(driver)
                last_id = inject_som(driver, start_id=1)
                if last_id > 1:
                    target_id = random.randint(0, last_id - 2)
                    element = driver.select(f'[data-ai-id="bid_{target_id}"]')
                    if element:
                        element.scroll_into_view()
            except Exception as e:
                pass

            driver.short_random_sleep()

            for _body_sel in ARTICLE_BODY_SELECTORS:
                if driver.is_element_present(_body_sel, wait=2):
                    element = driver.select(_body_sel)
                    if element:
                        element.scroll_into_view()
                    break

            raw_html, html_len = extract_hybrid_html(driver)
            b64_image = _capture_screenshot_b64(driver)

            if not local_meta.get("validation_passed"):
                skip_result = check_video_audio_skip(raw_html, url)
                if skip_result:
                    local_meta["retryable"] = False
                    _sync_meta("SKIPPED")
                    return skip_result

            if not local_meta.get("validation_passed"):
                pw_decision = check_paywall(raw_html, url, job_id, cancellation_flag)
                if pw_decision == "proceed":
                    wait_for_dom_stability(driver)
                    raw_html, _ = extract_hybrid_html(driver)
                    b64_image = _capture_screenshot_b64(driver)
                    local_meta["validation_passed"] = True
                    _sync_meta("RUNNING")
                elif pw_decision == "skip":
                    return {"status": "SKIPPED", "reason": "User skipped after HITL: Paywall detected"}
                elif pw_decision == "cancel":
                    return {"status": "CANCELED", "reason": "User requested stop via HITL."}

            if local_meta.get("validation_passed"):
                pass
            else:
                action, val_data = validate_article(raw_html, b64_image, url, sync_llm_chat)
                
                log.dual_log(
                    tag="Scraper:Validation:Verdict",
                    message=f"Validation verdict: {action.value}",
                    level="INFO" if action == ValidationAction.PROCEED else "WARNING",
                    payload={
                        "url": url,
                        "action": action.value,
                        "reason": val_data.get("reason", "") if val_data else "",
                        "valid": val_data.get("valid", None) if val_data else None,
                        "job_id": job_id,
                    }
                )
                
                if action == ValidationAction.AUTO_SKIP:
                    reason = val_data.get("reason", "unknown") if val_data else "unknown"
                    local_meta["retryable"] = False
                    _sync_meta("SKIPPED")
                    return {"status": "SKIPPED", "reason": f"Auto-skipped: {reason}"}
                elif action == ValidationAction.HUMAN_HELP:
                    reason = val_data.get("reason", "unknown") if val_data else "unknown"
                    if cancellation_flag is not None and cancellation_flag.is_set():
                        return {"status": "CANCELED", "reason": "User requested stop via Human Help mode."}
                    decision = _hitl_state.request_decision(job_id, url, f"BLOCKED PAGE - Action Required: {reason}")
                    if decision == "proceed":
                        wait_for_dom_stability(driver)
                        raw_html, _ = extract_hybrid_html(driver)
                        b64_image = _capture_screenshot_b64(driver)
                        local_meta["validation_passed"] = True
                        _sync_meta("RUNNING")
                    elif decision == "skip":
                        return {"status": "SKIPPED", "reason": f"User skipped after HITL: {reason}"}
                    elif decision == "cancel":
                        if cancellation_flag is not None:
                            cancellation_flag.set()
                        return {"status": "CANCELED", "reason": "User requested stop via HITL."}
                else:
                    local_meta["validation_passed"] = True
                    _sync_meta("RUNNING")

            result = summarize_article(raw_html, b64_image, url, driver, sync_llm_chat)
            if result["status"] == "SUCCESS":
                local_meta["summary_generated"] = True
                _sync_meta("RUNNING")
            return result

        except Exception as exc:
            log.dual_log(tag="Scraper:Process:Error", message="Error processing article", level="ERROR", exc_info=exc, payload={"url": url, "attempt": val_attempt, "error": str(exc)})

    return {"status": "FAILED", "reason": "Validation failed after 1 attempt"}
