# utils/startup/browser.py

import asyncio
from utils.logger.core import get_dual_logger
from utils.browser_daemon import get_or_create_driver
from utils.browser_lock import browser_lock

log = get_dual_logger(__name__)

async def warmup_browser() -> None:
    def _do_warmup():
        browser_lock.acquire()
        try:
            driver = get_or_create_driver()
            log.dual_log(tag="Startup:Browser", message="Warmup: waiting 5 seconds before navigation...", level="INFO")
            driver.short_random_sleep(5.0)  # Wait 5 seconds before navigation
            
            log.dual_log(tag="Startup:Browser", message="Navigating to example.com for warmup...", level="INFO")
            driver.get("https://example.com")
            driver.short_random_sleep()

            html = driver.page_html or ""
            if "Example Domain" not in html:
                raise RuntimeError("Browser warmup verification failed: 'Example Domain' not found in page HTML")

            log.dual_log(tag="Startup:Browser", message="Browser warmup verified successfully", level="INFO")
            return True
        finally:
            browser_lock.safe_release()

    try:
        result = await asyncio.wait_for(asyncio.to_thread(_do_warmup), timeout=35.0)  # Increased timeout to 35s
        if not result:
            raise RuntimeError("Browser warmup returned negative result")
    except asyncio.TimeoutError:
        raise RuntimeError("Browser warmup timed out after 35 seconds")
