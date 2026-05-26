# tools/scraper/resume.py
from typing import Any
from tools.base import BaseResumeHandler, ResumeReport
from database.connection import DatabaseManager
import json

class ResumeHandler(BaseResumeHandler):
    MIN_PENDING_URLS = 1
    
    def check_resume_state(self) -> ResumeReport:
        job_id = self.job_id
        target_site = self.args.get("target_site")
        
        if not target_site:
            return ResumeReport(tool_name="scraper", resumable=False, items_completed=0, items_pending=0, message="Cannot resume: No target_site in original job arguments.")
            
        conn = DatabaseManager.get_read_connection()
        job_row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        
        if not job_row:
            return ResumeReport(tool_name="scraper", resumable=False, items_completed=0, items_pending=0, message="Cannot resume: Job not found.")
            
        job_status = job_row["status"]
        if job_status in ("COMPLETED", "ABANDONED", "SKIPPED"):
            return ResumeReport(tool_name="scraper", resumable=False, items_completed=0, items_pending=0, message=f"Cannot resume: Job already {job_status}.")
            
        scraper_items = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM job_items WHERE job_id = ? AND json_extract(item_metadata, '$.step') = 'scrape' GROUP BY status",
            (job_id,)
        ).fetchall()
        
        counts = {r["status"]: r["cnt"] for r in scraper_items}
        completed_urls = counts.get("COMPLETED", 0)
        failed_urls = counts.get("FAILED", 0)
        pending_urls = counts.get("PENDING", 0)
        
        items_pending = pending_urls + failed_urls
        is_resumable = items_pending >= self.MIN_PENDING_URLS or completed_urls == 0
        
        msg = f"Scraper ready for resumption on {target_site}. {completed_urls} completed, {items_pending} pending."
        
        return ResumeReport(
            tool_name="scraper",
            resumable=is_resumable,
            items_completed=completed_urls,
            items_pending=items_pending,
            message=msg,
            details={"job_id": job_id, "target_site": target_site, "job_status": job_status, "completed": completed_urls, "pending": pending_urls, "failed": failed_urls}
        )
