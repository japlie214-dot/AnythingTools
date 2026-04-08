# tools/scraper/browser.py
"""Browser helper utilities: selector polling, screenshot capture, and
multimodal LLM message assembly."""

import os
import time
import base64
from botasaurus.browser import Driver
from utils.logger import get_dual_logger
from bs4 import BeautifulSoup
import config

log = get_dual_logger(__name__)


def _wait_for_any_selector(
    driver: Driver,
    selectors: list[str],
    timeout: float = 15.0,
) -> bool:
    """Poll until any selector matches or timeout elapses. Returns True on match."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                if driver.is_element_present(sel):
                    return True
            except Exception:
                continue
        driver.short_random_sleep()
    return False


def _capture_screenshot_b64(driver: Driver) -> str | None:
    """Best-effort screenshot. Returns a Base64 PNG string or None on failure."""
    try:
        tmp_dir = "data/temp"
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, f"scr_{int(time.time() * 1000)}.png")
        driver.save_screenshot(tmp_path)
        with open(tmp_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("utf-8")
        os.remove(tmp_path)
        return b64
    except Exception as exc:
        log.dual_log(
            tag="Scraper:Screenshot",
            message=f"Screenshot capture failed (non-fatal): {exc}",
            level="WARNING",
        )
        return None


def _build_multimodal_messages(
    system_prompt: str,
    html_text: str,
    b64_image: str | None,
) -> list[dict]:
    """Assemble an LLM message list with text and optional vision parts."""
    user_parts: list[dict] = [{"type": "text", "text": f"HTML:\n{html_text}"}]
    if b64_image:
        user_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64_image}"},
        })
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_parts},
    ]


from utils.browser_utils import extract_hybrid_html as centralized_extract


def extract_hybrid_html(driver: Driver) -> tuple[str, int]:
    """
    Surgical extraction using Botasaurus driver.page_html.
    """
    try:
        html = driver.page_html or ""
        result_text = centralized_extract(html)
        return result_text, len(result_text)

    except Exception as e:
        log.dual_log(tag="Scraper:Extract:Hybrid", message=f"Hybrid extraction failed: {e}", level="WARNING", exc_info=e)
        return "INSUFFICIENT_CONTENT", 0
