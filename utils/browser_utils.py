# utils/browser_utils.py
from botasaurus.browser import Driver
from utils.logger import get_dual_logger
from bs4 import BeautifulSoup
# removed unused 'threading' import; navigation is performed synchronously in safe_google_get
import time
from urllib.parse import urlparse

_log = get_dual_logger(__name__)


def safe_google_get(driver: Driver, url: str, *, bypass_cloudflare: bool = True) -> None:
    """Stealth-aware synchronous wrapper around driver.google_get()."""
    _log.dual_log(tag="Browser:Navigate", message=f"Navigating to: {url}", payload={"url": url})
    start_t = time.monotonic()
    
    for attempt in range(2):
        from utils.browser_daemon import daemon_manager
        if not daemon_manager.is_driver_alive():
            _log.dual_log(tag="Browser:Navigate:Liveness", message="Driver dead before navigation, re-initializing", level="WARNING", payload={"url": url})
            driver = daemon_manager.get_or_create_driver()

        try:
            try:
                driver.google_get(url, bypass_cloudflare=bypass_cloudflare)
            except TypeError as exc:
                if "bypass_cloudflare" not in str(exc):
                    raise
                driver.google_get(url)
        except Exception as e:
            if attempt == 0:
                continue
            raise e

        # MANDATORY POST-NAV STABILIZATION
        driver.sleep(3)
        driver.short_random_sleep()

        # NAVIGATION VERIFICATION
        try:
            actual = getattr(driver, "current_url", None) if hasattr(driver, "current_url") else driver.get_current_url()
            if actual and urlparse(url).netloc == urlparse(actual).netloc:
                break
            else:
                if attempt == 0:
                    _log.dual_log(tag="Browser:Navigate", message="URL mismatch detected, retrying", level="WARNING", payload={"url": url, "actual": actual})
                    continue
                else:
                    raise RuntimeError(f"Navigation verification failed: expected {url}, got {actual}")
        except Exception as e:
            if attempt == 0:
                continue
            raise e

    elapsed = time.monotonic() - start_t
    _log.dual_log(tag="Browser:Navigate", message=f"Navigation completed: {url} ({elapsed:.2f}s)", payload={"url": url, "elapsed_s": round(elapsed, 2)})

    try:
        from utils.som_utils import enforce_single_tab
        enforce_single_tab(driver)
    except Exception as e:
        _log.dual_log(tag="Browser:Utils", message=f"enforce_single_tab failed: {e}", level="WARNING", payload={"error": str(e)})


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
