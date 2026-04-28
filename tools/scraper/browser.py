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
    Greedy extraction: Captures leaf-node content containers with >40 characters.
    """
    try:
        html_content = driver.page_html or ""
        if not html_content or len(html_content.strip()) < 100:
            return "INSUFFICIENT_CONTENT", 0
        
        soup = BeautifulSoup(html_content, "html.parser")
        
        # 1. Strip structural noise & iframes entirely
        for tag in soup.find_all(["iframe", "script", "style", "noscript", "header", "footer", "nav"]):
            tag.decompose()
            
        # 2. Minimize attributes globally to prevent descendants from leaking noise
        allowed_attrs = {"href", "data-ai-id", "browsergym_set_of_marks", "browsergym_visibility_ratio"}
        for tag in soup.find_all(True):
            tag.attrs = {k: v for k, v in tag.attrs.items() if k in allowed_attrs}
            
        fragments = []
        captured_node_ids = set() # Tracks `id()` of captured elements
        
        for element in soup.find_all(True):
            if not element.parent:
                continue
                
            # --- ANCESTOR CHECK ---
            # If any parent of this element was already captured, skip it to prevent duplication
            parent = element.parent
            is_already_captured = False
            while parent:
                if id(parent) in captured_node_ids:
                    is_already_captured = True
                    break
                parent = parent.parent
                
            if is_already_captured:
                continue
            # ----------------------
                
            node_text = element.get_text(strip=True)
            text_length = len(node_text)
            
            # Ignore tiny fragments
            if text_length <= 40:
                continue
            
            # --- WRAPPER DETECTION ---
            # Sum the text length of all DIRECT children.
            # If direct children account for >80% of this element's text, it's just a structural wrapper.
            direct_children_text_len = sum(
                len(child.get_text(strip=True)) 
                for child in element.find_all(True, recursive=False)
            )
            
            if direct_children_text_len > (text_length * 0.8):
                continue # Skip wrapper; the loop will naturally process its children.
            # -------------------------
            
            # This is a leaf-node container! Capture it and protect its descendants.
            captured_node_ids.add(id(element))
            
            fragments.append(str(element))
        
        # Join cleanly with newlines
        result = "\n".join(fragments)
        
        MAX_CONTEXT = 60000
        if len(result) > MAX_CONTEXT:
            result = result[:MAX_CONTEXT] + "\n... [TRUNCATED]"
            
        if len(result) < 100:
            return "INSUFFICIENT_CONTENT", 0
            
        return result, len(result)
        
    except Exception as e:
        log.dual_log(
            tag="Scraper:Extract:Hybrid",
            message=f"Greedy extraction failed: {e}",
            level="ERROR",
            exc_info=e
        )
        return "INSUFFICIENT_CONTENT", 0
