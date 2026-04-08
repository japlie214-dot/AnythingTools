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


def extract_hybrid_html(driver: Driver) -> tuple[str, int]:
    """
    Surgical extraction using Botasaurus driver.page_html.
    Filters for granular elements > 40 chars while deduplicating
    by selecting innermost content nodes only. Interactive elements
    (data-ai-id) are always preserved for the Recovery agent.
    Produces an audit payload describing kept elements and truncates
    at element boundaries when exceeding budget.
    """
    try:
        html = driver.page_html or ""
        if not html or not html.strip():
            log.dual_log(tag="Scraper:Extract:EmptyHybrid", message="Hybrid extractor received empty driver.page_html", level="WARNING")
            return "INSUFFICIENT_CONTENT", 0

        soup = BeautifulSoup(html, "html.parser")

        # Remove obvious non-content nodes
        for noise in soup(["script", "style", "link", "meta", "noscript", "svg", "iframe", "header", "footer", "nav"]):
            try:
                noise.decompose()
            except Exception:
                continue

        captured_chunks: list[str] = []
        kept_elements_audit: list[dict] = []

        # Innermost-only selection: capture elements with >40 chars whose
        # direct children do not themselves contain >40 chars. Always keep interactive elements
        for element in soup.find_all(True):
            is_interactive = element.has_attr("data-ai-id")
            text = element.get_text(separator=" ", strip=True)

            if not is_interactive and len(text) <= 40:
                continue

            # Deduplication: skip containers if they have content-heavy direct children
            if not is_interactive:
                has_content_child = False
                for child in element.find_all(True, recursive=False):
                    if len(child.get_text(separator=" ", strip=True)) > 40:
                        has_content_child = True
                        break
                if has_content_child:
                    continue

            # Keep only safe attributes to reduce token cost
            allowed_attrs = {"href", "data-ai-id"}
            attrs = dict(element.attrs)
            element.attrs = {k: v for k, v in attrs.items() if k in allowed_attrs}

            # Normalize whitespace and append
            chunk = str(element).replace("\n", " ")
            captured_chunks.append(chunk)

            kept_elements_audit.append({
                "tag": element.name,
                "text_len": len(text),
                "is_interactive": is_interactive,
                "snippet": text[:100] + ("..." if len(text) > 100 else "")
            })

        # Audit log for transparency (what we kept)
        try:
            log.dual_log(
                tag="Scraper:SurgicalCapture",
                message=f"Surgical extraction kept {len(captured_chunks)} elements.",
                level="INFO",
                payload={"elements_kept": kept_elements_audit},
            )
        except Exception:
            # Non-fatal logging failure should not break extraction
            pass

        result_text = "\n".join(captured_chunks)

        budget = getattr(config, "BROWSER_SOM_HTML_CHAR_BUDGET", 20000)
        if len(result_text) > budget:
            # Truncate at the last newline to avoid cutting in the middle of a tag
            last_newline = result_text.rfind("\n", 0, budget)
            cutoff = last_newline if last_newline > 0 else budget
            result_text = result_text[:cutoff] + "\n...[TRUNCATED]"

        if not result_text.strip():
            log.dual_log(tag="Scraper:Extract:EmptyHybrid", message="Surgical extraction returned 0 chars from page_html", level="WARNING")
            return "INSUFFICIENT_CONTENT", 0

        return result_text, len(result_text)

    except Exception as e:
        log.dual_log(tag="Scraper:Extract:Hybrid", message=f"Hybrid extraction failed: {e}", level="WARNING", exc_info=e)
        return "INSUFFICIENT_CONTENT", 0
