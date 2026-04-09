# tools/actions/browser/browser_operator/tool.py
import asyncio
from typing import Any
from tools.base import BaseTool
from database.job_queue import update_job_heartbeat
from utils.browser_daemon import get_or_create_driver
from utils.browser_utils import safe_google_get, extract_hybrid_html
from utils.tracker import TestTracker

class BrowserOperator(BaseTool):
    name = "browser:operator"

    async def run(self, args: dict[str, Any], telemetry, **kwargs) -> str:
        """Execute the browser operator tool.
        Returns a JSON‑encoded string as required by BaseTool.run contract.
        """
        from utils.browser_lock import browser_lock
        import json
        
        # Concurrency guard – ensure only one browser task runs at a time.
        if browser_lock.locked():
            return json.dumps({"status": "FAILED", "message": "System busy: another browser task is running."})
        await browser_lock.acquire()
        try:
            job_id = kwargs.get("job_id")
            if not job_id:
                return json.dumps({"status": "FAILED", "message": "Error: No job_id provided for browser operator."})
            meta = args.get("_client_metadata", {})
            tracker = TestTracker(job_id, meta.get("enable_tracker", False))
            
            driver = get_or_create_driver()
            target = args.get("target")
            
            if target:
                safe_google_get(driver, target)
                tracker.capture_milestone("Initial Load", driver.page_html or "")
            
            # Placeholder interaction loop – heartbeat & logging.
            for _ in range(1):
                update_job_heartbeat(job_id)
                tracker.log_step("ACTION: visit_target", "Navigator")
                await asyncio.sleep(1)
            
            tracker.capture_milestone("Success Declaration", driver.page_html or "")
            return json.dumps({"status": "COMPLETED", "message": "Browser operations finished"})
        finally:
            browser_lock.release()
        
    def extract_page_content(self, driver) -> str:
        """Extracts the readable text content from the current webpage's HTML."""
        html = driver.page_html or ""
        return extract_hybrid_html(html, limit=400000)
