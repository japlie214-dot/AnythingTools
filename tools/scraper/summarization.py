# tools/scraper/summarization.py
"""Article summarization: LLM-based structured summary generation with retry."""

from tools.scraper.browser import (
    _safe_wait_for_any_selector,
    _capture_screenshot_b64,
    extract_hybrid_html,
    _build_multimodal_messages,
)
from tools.scraper.targets import ARTICLE_BODY_SELECTORS
from tools.scraper.scraper_prompts import SUMMARIZATION_PROMPT, SUMMARIZATION_SCHEMA
from utils.text_processing import parse_llm_json, escape_prompt_separators
from utils.som_utils import wait_for_dom_stability
from utils.browser_utils import safe_google_get
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def summarize_article(raw_html: str, b64_image: str | None, url: str, driver, sync_llm_chat) -> dict:
    slim_sum = raw_html

    for sum_attempt in range(1, 4):
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
                log.dual_log(tag="Scraper:Summarize:ContextLength", message="Context length exceeded during json_schema call", level="WARNING", payload={"error": str(format_exc)})
                raise

            if isinstance(format_exc, BadRequestError):
                log.dual_log(tag="Scraper:Summarize:Fallback", message="json_schema format rejected; falling back to json_object", level="WARNING", payload={"error": str(format_exc)})
                sum_resp = sync_llm_chat(sum_msgs, response_format={"type": "json_object"})
                used_format = "json_object"
            else:
                raise

        sum_data = parse_llm_json(sum_resp.content or "{}")

        log.dual_log(
            tag="Scraper:Summarize:Response",
            message=f"LLM summarization response received via {used_format}",
            payload={"url": url, "raw_content": sum_resp.content, "raw_len": len(sum_resp.content or ""), "sum_attempt": sum_attempt, "parsed_keys": list(sum_data.keys())},
        )

        if sum_data.get("error") == "INSUFFICIENT_CONTENT":
            log.dual_log(tag="Scraper:Summarization:Error", message="Insufficient content", level="WARNING", payload={"attempt": sum_attempt, "url": url})
        elif sum_data.get("title") and sum_data.get("conclusion"):
            log.dual_log(tag="Scraper:Summarization:Success", message=f"Generated structured summary for {url}", payload={"url": url, "title": sum_data.get("title", "")[:50]})
            return {"status": "SUCCESS", "parsed_json": sum_data}
        else:
            log.dual_log(tag="Scraper:Summarization:Error", message="Missing mandatory fields in JSON", level="WARNING", payload={"parsed": sum_data, "attempt": sum_attempt, "url": url})

        if sum_attempt < 3:
            log.dual_log(tag="Scraper:Navigation:Retry", message="Re-navigating for summarisation retry", level="INFO", payload={"url": url, "sum_attempt": sum_attempt})
            safe_google_get(driver, url)
            _safe_wait_for_any_selector(driver, ARTICLE_BODY_SELECTORS, timeout=15)
            wait_for_dom_stability(driver)
            driver.scroll_to_bottom()
            
            for _body_sel in ARTICLE_BODY_SELECTORS:
                if driver.is_element_present(_body_sel, wait=2):
                    element = driver.select(_body_sel)
                    if element:
                        element.scroll_into_view()
                    break
            raw_html, _ = extract_hybrid_html(driver)
            b64_image = _capture_screenshot_b64(driver)
            slim_sum  = raw_html

    return {"status": "FAILED", "reason": "Summarisation returned empty or insufficient content after 3 attempts"}
