# utils/browser_daemon.py
# NOTE: This is a personal project for a single user in Windows.
import os
import collections
from datetime import datetime, timezone
from botasaurus.browser import Driver, cdp
import config
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

# ── Shared module-level state ─────────────────────────────────────────────────
_driver: Driver | None = None

# Persistent SoM element-range registry.  Cleared on navigate, repopulated by
# browser_get_state via reinject_all().  Passed by reference to som_utils
# functions so iframe context resolution works across separate tool calls.
_id_tracking: dict = {}

# Rolling action log: scalar summaries only — no HTML or DOM content.
_action_log: collections.deque = collections.deque(maxlen=50)


# ── Driver lifecycle ─────────────────────────────────────────────────────────-
def _init_driver() -> Driver:
    """Create and configure the singleton Driver.  Called only by get_or_create_driver()."""
    global _driver
    # Direct instantiation mirrors @browser decorator kwargs used in task.py.
    # Botasaurus >= 4.0 supports these kwargs on the Driver constructor.
    _driver = Driver(
        headless=False,
        user_agent="real",
        window_size=(1920, 1080),
        arguments=[f"--user-data-dir={os.path.abspath(config.CHROME_USER_DATA_DIR)}"],
    )
    # IMPORTANT DEVELOPER REMINDER (permanent):
    # The CHROME_USER_DATA_DIR profile MUST be manually configured so its
    # default download directory points to the absolute path of `chrome_download`.
    # All CDP download overrides have been removed because they are unsupported
    # in this Botasaurus version.
    _id_tracking.clear()  # new session means no valid SoM ranges
    return _driver


def is_driver_alive() -> bool:
    """Lightweight health check.  Must NOT be called while browser_lock is held
    by the caller — callers must own the lock before calling get_or_create_driver()."""
    global _driver
    if _driver is None:
        return False
    try:
        _driver.run_js("return 1;")
        return True
    except Exception:
        return False


def get_or_create_driver() -> Driver:
    """Return the live singleton Driver, re-initialising if the session is dead.
    CALLER MUST HOLD browser_lock before invoking this function."""
    global _driver
    if _driver is None or not is_driver_alive():
        log.dual_log(tag="Browser:Daemon", message="Initialising new Driver session.")
        if _driver is not None:
            try:
                _driver.close()
                _driver.quit()
            except Exception:
                pass
        _init_driver()
    return _driver


def shutdown_driver() -> None:
    """Gracefully close and quit the Driver.  Called from post_stop in main.py."""
    global _driver
    if _driver is not None:
        try:
            _driver.close()
            _driver.quit()
        except Exception:
            pass
        _driver = None
        log.dual_log(tag="Browser:Daemon", message="Driver shut down.")


# ── id_tracking accessors ─────────────────────────────────────────────────────
def get_id_tracking() -> dict:
    """Return the shared SoM id-tracking dict by reference.
    Callers must hold browser_lock before modifying it."""
    return _id_tracking


# ── Action Logger accessors ───────────────────────────────────────────────────
def append_action_log(entry: dict) -> None:
    """Append a sanitised entry to the rolling log.
    collections.deque.append() is atomic under CPython's GIL; no extra lock needed."""
    safe = {
        "tool_name":    str(entry.get("tool_name",    ""))[:200],
        "args_summary": {str(k): str(v)[:200]
                         for k, v in entry.get("args_summary", {}).items()},
        "outcome":      str(entry.get("outcome",      ""))[:200],
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    }
    _action_log.append(safe)


def get_action_log_snapshot(count: int) -> list[dict]:
    """Thread-safe snapshot of the last *count* entries (GIL-safe list copy)."""
    return list(_action_log)[-max(1, min(count, 50)):]
