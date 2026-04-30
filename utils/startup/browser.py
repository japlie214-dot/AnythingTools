# utils/startup/browser.py

import asyncio
import sys
from utils.logger.core import get_dual_logger
from utils.browser_lock import browser_lock
from utils.browser_daemon import daemon_manager

log = get_dual_logger(__name__)


async def warmup_browser() -> None:
    """CRITICAL: Deep warmup orchestration. Fatal on failure."""
    def _do_warmup():
        browser_lock.acquire()
        try:
            return daemon_manager.deep_warmup()
        finally:
            browser_lock.safe_release()

    try:
        # Expand timeout to 60 seconds to accommodate stabilization delay and slow cold-starts
        success = await asyncio.wait_for(asyncio.to_thread(_do_warmup), timeout=60.0)
        if not success:
            raise RuntimeError("Browser failed internal health checks.")
    except asyncio.TimeoutError:
        log.dual_log(tag="Startup:Browser", message="Browser warmup timed out.", level="ERROR", payload={"timeout_s": 60})
        raise RuntimeError("Browser warmup timed out after 90 seconds.")
    except Exception as e:
        log.dual_log(tag="Startup:Browser", message=f"Warmup crashed: {e}", level="CRITICAL", payload={"error": str(e)})
        raise RuntimeError(f"Browser Warmup Failed: {e}")
