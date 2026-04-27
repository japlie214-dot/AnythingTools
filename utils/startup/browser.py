# utils/startup/browser.py

import asyncio
import sys
from utils.logger.core import get_dual_logger
from utils.browser_lock import browser_lock
from utils.browser_daemon import daemon_manager

log = get_dual_logger(__name__)


async def warmup_browser() -> None:
    """
    Deep warmup orchestration with failure policy.
    On failure: logs warning and triggers sys.exit(1).
    """
    def _do_warmup():
        browser_lock.acquire()
        try:
            # Run deep warmup via daemon manager (async)
            import asyncio
            return asyncio.run(daemon_manager.deep_warmup())
        finally:
            browser_lock.safe_release()

    try:
        # Increased timeout to 60s to accommodate deep stack verification
        success = await asyncio.wait_for(asyncio.to_thread(_do_warmup), timeout=60.0)
        if not success:
            log.dual_log(tag="Startup:Browser", message="Deep Warmup failed. Shutting down.", level="CRITICAL")
            sys.exit(1)
    except asyncio.TimeoutError:
        log.dual_log(tag="Startup:Browser", message="Deep Warmup timed out after 60 seconds. Shutting down.", level="CRITICAL")
        sys.exit(1)
    except Exception as e:
        log.dual_log(tag="Startup:Browser", message=f"Warmup process crashed: {e}", level="CRITICAL")
        sys.exit(1)
