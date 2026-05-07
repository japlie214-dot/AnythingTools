# tools/scraper/resume.py
import json
from typing import Any
from tools.base import BaseResumeHandler, ResumeReport
from database.connection import DatabaseManager

class ResumeHandler(BaseResumeHandler):
    def check_resume_state(self) -> ResumeReport:
        conn = DatabaseManager.get_read_connection()
        rows = conn.execute(
            "SELECT status FROM job_items WHERE job_id = ? AND json_extract(item_metadata, '$.step') = 'scrape'",
            (self.job_id,)
        ).fetchall()
        
        completed = sum(1 for r in rows if r["status"] == "COMPLETED")
        pending = sum(1 for r in rows if r["status"] in ("PENDING", "FAILED"))
        needs_link_extraction = len(rows) == 0
        
        msg = f"Resuming Scraper. {completed} URLs completed, {pending} pending."
        if needs_link_extraction:
            msg += " Link extraction required."
        else:
            msg += " Link extraction will be bypassed."
            
        return ResumeReport(
            tool_name="scraper",
            resumable=True,
            items_completed=completed,
            items_pending=pending,
            message=msg,
            details={"needs_link_extraction": needs_link_extraction},
        )
