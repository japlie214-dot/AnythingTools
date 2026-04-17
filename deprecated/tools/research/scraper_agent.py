# deprecated/tools/research/scraper_agent.py
"""
Agentic Browser Scraper with Intelligent Recovery.

This module provides a more robust scraping mechanism with automatic
detection and recovery from common blockers like CAPTCHAs, cookie walls,
and Cloudflare protections.
"""

import os
import time
import random
from typing import Optional

from botasaurus.browser import browser, Driver
from utils.logger import get_dual_logger
from utils.browser_utils import safe_google_get
from utils.som_utils import inject_som, wait_for_dom_stability
from tools.research.blocker_detection import is_hard_blocked
from tools.scraper.browser import extract_hybrid_html
from tools.research.mechanical_bypass import attempt_mechanical_bypass
from tools.research.agentic_loop import agentic_recovery_loop
from utils.som_utils import extract_surgical_html
from utils.hitl import pause_for_hitl

log = get_dual_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# BOTASAURUS INTERACTION POLICY — READ BEFORE ADDING ANY BROWSER ACTION
# ═══════════════════════════════════════════════════════════════════════════
# ALL click / scroll / type / navigate actions MUST use Botasaurus native API.
# See PLAN-02 or the Botasaurus README for the full method reference.
# run_js() is ONLY permitted for DOM mutations that have no native equivalent:
#   data-ai-id injection, badge overlay management, MutationObserver,
#   and getBoundingClientRect/elementFromPoint occlusion tests.
# If you reach for run_js() for a click, scroll, or type — STOP.
# ═══════════════════════════════════════════════════════════════════════════



class AgenticBrowserScraper:
    def __init__(self, max_retries: int = 3, base_delay: float = 2.0, headless: bool = False):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._topic_hint: str = ''
        self._headless: bool = headless   # ← stored here; never read from driver.config

    def _enrich_content(self, driver: Driver) -> str:
        try:
            # Organic timing
            driver.short_random_sleep()
            # Botasaurus native scroll — scroll_to_bottom() is always available;
            # never use run_js("window.scrollTo(...)") as a fallback.
            driver.scroll_to_bottom()
            driver.short_random_sleep()
            # SoM-based engagement scroll instead of prohibited window.scrollBy
            try:
                wait_for_dom_stability(driver)
                last_id = inject_som(driver, start_id=1)
                if last_id > 1:
                    target_id = random.randint(1, last_id - 1)
                    # Botasaurus native scroll — never use run_js for scrollIntoView.
                    element = driver.select(f'[data-ai-id="{target_id}"]')
                    if element:
                        element.scroll_into_view()
            except Exception as engagement_error:
                log.dual_log(
                    tag="Research:Scraper",
                    message=f"Engagement scroll failed: {engagement_error}",
                    level="DEBUG",
                )
        except Exception as e:
            log.dual_log(
                tag="Research:Scraper",
                message=f"Scroll enrichment failed: {e}",
                level="WARNING",
                payload={"error": repr(e)},
            )
        html_content, _ = extract_hybrid_html(driver)
        return html_content

    def scrape(self, url: str, topic_hint: str = '', cancellation_flag=None) -> str:
        # Store the topic hint for potential LLM recovery
        self._topic_hint = topic_hint
        # Store flag on instance so _scrape_with_recovery can access it via closure,
        # consistent with self._topic_hint pattern; avoids threading.Event in task_data.
        self._cancellation_flag = cancellation_flag

        import config as _cfg  # local import avoids circular dependency at module level
        @browser(
            headless=False,
            reuse_driver=True,
            user_agent='real',                # botasaurus selects a realistic hashed UA
            window_size=(1920, 1080),
            add_arguments=[f"--user-data-dir={os.path.abspath(_cfg.CHROME_USER_DATA_DIR)}"]
        )
        def _scrape_with_recovery(driver: Driver, task_data: dict) -> str:
            target_url = task_data.get('url')
            # self._cancellation_flag accessed via closure — set by scrape() before this call.
            raw_html = ""
            for attempt in range(self.max_retries):
                if self._cancellation_flag is not None and self._cancellation_flag.is_set():
                    return "__CANCELED__"
                try:
                    log.dual_log(
                        tag="Research:Scraper",
                        message=f"Scrape attempt {attempt + 1} for {target_url}",
                        level="INFO",
                        payload={"attempt": attempt + 1, "url": target_url},
                    )
                    # safe_google_get — stealth referral with TypeError-safe fallback
                    safe_google_get(driver, target_url)
                    driver.long_random_sleep()
                    raw_html = self._enrich_content(driver)
                    if is_hard_blocked(raw_html):
                        log.dual_log(
                            tag="Research:Scraper",
                            message=f"Blocker detected on attempt {attempt + 1}",
                            level="WARNING",
                            payload={"attempt": attempt + 1},
                        )
                        if self.recover_blocker(driver, self._topic_hint, self._cancellation_flag):
                            raw_html = self._enrich_content(driver)
                        else:
                            # Explicit failure — never return silent empty string
                            return (
                                "❌ Scraping failed: Unresolvable blocker encountered. "
                                "AI Navigator exhausted all attempts."
                            )
                    if not is_hard_blocked(raw_html):
                        break
                except Exception as e:
                    if str(e).startswith("PAUSED_FOR_HITL:"):
                        raise
                    log.dual_log(
                        tag="Research:Scraper",
                        message=f"Scrape attempt {attempt + 1} failed: {e}",
                        level="ERROR",
                        payload={"attempt": attempt + 1, "url": target_url, "error": repr(e)},
                        exc_info=e,
                    )
                    if attempt < self.max_retries - 1:
                        driver.long_random_sleep()
                    continue

            if not raw_html or is_hard_blocked(raw_html):
                log.dual_log(
                    tag="Research:Scraper",
                    message=f"Failed to scrape {target_url} after {self.max_retries} attempts",
                    level="ERROR",
                    payload={"url": target_url, "attempts": self.max_retries},
                )
                return "❌ Scraping failed: Unresolvable blocker encountered. AI Navigator exhausted all attempts."

            from utils.text_processing import clean_html_for_agent
            slim_html = clean_html_for_agent(raw_html, max_chars=50000)
            final_text = slim_html
            # slim_html logged as payload; if > LOGGER_TRUNCATION_LIMIT the masking
            # layer in FileFormatter will emit [MASKED: ...] automatically.
            log.dual_log(
                tag="Research:Scraper:Extract",
                message=f"Extracted cleaned text ({len(final_text)} chars).",
                payload=slim_html,
            )
            return final_text

        return _scrape_with_recovery({'url': url})


    def recover_blocker(self, driver: Driver, topic_hint: str, cancellation_flag=None) -> bool:
        """Canonical three-stage recovery sequence: Mechanical → AI Navigator → HITL.

        Returns True if the page is unblocked at any stage, False only when all
        stages are exhausted (or HITL is unavailable in headless mode).
        Accepts an optional threading.Event; sets it and returns False immediately
        if the operator types the exact string 'Stop' at the HITL prompt.
        """
        log.dual_log(
            tag="Research:Scraper",
            message="Starting canonical recovery sequence.",
            level="INFO",
        )

        _403_signals = [
            "access error", "potential misuse", "access blocked",
            "status code: 403", "<debug-panel>",
        ]
        _html_lower = (driver.page_html or "").lower()
        is_severe_403 = any(sig in _html_lower for sig in _403_signals)

        if is_severe_403:
            log.dual_log(
                tag="Research:Scraper",
                message="Severe 403 block detected. Fast-tracking to Stage 3 (HITL).",
                level="WARNING",
            )
        else:
            # ── Stage 1: Mechanical Bypass ────────────────────────────────────────
            if attempt_mechanical_bypass(driver, self._headless):
                driver.short_random_sleep()
                if not is_hard_blocked(extract_surgical_html(driver)):
                    log.dual_log(tag="Research:Scraper", message="Mechanical bypass succeeded.", level="INFO")
                    return True
            # ── Stage 2: AI Navigator (max 3 steps) ──────────────────────────────
            if agentic_recovery_loop(driver, topic_hint, max_steps=3, cancellation_flag=cancellation_flag, headless=self._headless):
                driver.short_random_sleep()
                if not is_hard_blocked(extract_surgical_html(driver)):
                    log.dual_log(tag="Research:Scraper", message="AI Navigator succeeded.", level="INFO")
                    return True
        # Guard: if the operator typed "Stop" at Stage 2's human_help prompt,
        # the flag is already set; return immediately rather than entering Stage 3.
        if cancellation_flag is not None and cancellation_flag.is_set():
            return False
        # ── Stage 3: HITL Terminal Pause (headed mode only, infinite loop) ───
        if self._headless:
            log.dual_log(
                tag="Research:Scraper",
                message="Headless mode: HITL unavailable. Recovery sequence exhausted.",
                level="ERROR",
            )
            return False

        # Use pause_for_hitl instead of infinite input loop
        pause_for_hitl("BLOCKER DETECTED: Please resolve the challenge (CAPTCHA / Cloudflare / Paywall) in the browser.")
        return False
