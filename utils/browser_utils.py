# utils/browser_utils.py
from botasaurus.browser import Driver
from utils.logger import get_dual_logger
from bs4 import BeautifulSoup

_log = get_dual_logger(__name__)


def safe_google_get(driver: Driver, url: str, *, bypass_cloudflare: bool = True) -> None:
    """Stealth-aware wrapper around driver.google_get()."""
    _log.dual_log(tag="Browser:Navigate", message=f"Navigating to: {url}", payload={"url": url})
    try:
        driver.google_get(url, bypass_cloudflare=bypass_cloudflare)
        # Enforce single-tab defensively after navigation.
        try:
            from utils.som_utils import enforce_single_tab
            try:
                enforce_single_tab(driver)
            except Exception as e:
                _log.dual_log(tag="Browser:Utils", message=f"enforce_single_tab failed after google_get: {e}", level="WARNING", exc_info=e)
        except Exception:
            # Import may fail in constrained environments; ignore and continue.
            pass
    except TypeError as exc:
        if "bypass_cloudflare" not in str(exc):
            raise
        _log.dual_log(
            tag="Browser:Utils",
            message=(
                "bypass_cloudflare kwarg unsupported by installed Botasaurus. "
                "Falling back to bare google_get(). Upgrade Botasaurus for stealth mode."
            ),
            level="WARNING",
            payload={"url": url},
        )
        driver.google_get(url)


def extract_hybrid_html(html_content: str, limit: int = 400000) -> str:
    """Centralized Readability Engine."""
    if not html_content:
        return "INSUFFICIENT_CONTENT"
    soup = BeautifulSoup(html_content, "html.parser")
    for noise in soup(["script", "style", "link", "meta", "noscript", "svg", "iframe", "header", "footer", "nav"]):
        try:
            noise.decompose()
        except Exception:
            continue

    captured_chunks = []
    for element in soup.find_all(True):
        is_interactive = element.has_attr("data-ai-id") or element.has_attr("browsergym_set_of_marks")
        text = element.get_text(separator=" ", strip=True)
        if not is_interactive and len(text) <= 40:
            continue

        if not is_interactive:
            has_content_child = any(len(child.get_text(separator=" ", strip=True)) > 40 for child in element.find_all(True, recursive=False))
            if has_content_child:
                continue

        # Strip attributes to minimize token usage
        allowed_attrs = {"href", "data-ai-id", "browsergym_set_of_marks", "browsergym_visibility_ratio"}
        attrs = dict(element.attrs)
        element.attrs = {k: v for k, v in attrs.items() if k in allowed_attrs}

        captured_chunks.append(str(element).replace("\n", " "))

    result = "\n".join(captured_chunks)
    if len(result) > limit:
        return result[:limit] + "\n[SYSTEM: Content truncated due to length.]"
    return result or "INSUFFICIENT_CONTENT"
