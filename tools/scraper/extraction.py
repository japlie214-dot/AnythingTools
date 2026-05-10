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

        _safe_wait_for_any_selector(driver, target["selectors"], timeout=15)
        wait_for_dom_stability(driver)
        log.dual_log(tag="Scraper:Scroll", message="Scrolling to bottom", level="INFO", payload={"url": target["url"]})
        driver.scroll_to_bottom()
        log.dual_log(tag="Scraper:Scroll", message="Scroll to bottom completed", level="INFO", payload={"url": target["url"]})
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


# Backward-compatible re-exports for existing importers
from tools.scraper.article_processor import process_article
from tools.scraper.hitl import HITLState, ValidationAction, _hitl_state
