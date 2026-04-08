# utils/browser_utils.py
from botasaurus.browser import Driver
from utils.logger import get_dual_logger

_log = get_dual_logger(__name__)


def safe_google_get(driver: Driver, url: str, *, bypass_cloudflare: bool = True) -> None:
    """Stealth-aware wrapper around driver.google_get().

    Attempts the call with bypass_cloudflare=True (Botasaurus ≥ street-smarts
    builds). On TypeError caused specifically by the unrecognised kwarg —
    indicating an older Botasaurus build — logs a WARNING and retries with the
    bare signature so the run continues in degraded mode rather than crashing.
    Any other TypeError (e.g. url is None, driver is uninitialised) is re-raised
    immediately to avoid masking real bugs.
    """
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
