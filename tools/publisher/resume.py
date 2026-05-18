# tools/publisher/resume.py
import json
from typing import Any
from tools.base import BaseResumeHandler, ResumeReport
from database.broadcast.queries import get_batch_info, get_batch_publish_progress

class ResumeHandler(BaseResumeHandler):
    def check_resume_state(self) -> ResumeReport:
        batch_id = self.args.get("batch_id")
        if not batch_id:
            return ResumeReport("publisher", False, 0, 0, "No batch_id provided in arguments.")
            
        batch_info = get_batch_info(batch_id)
        if not batch_info:
            return ResumeReport("publisher", False, 0, 0, "Batch not found in broadcast_batches.")

        progress = get_batch_publish_progress(batch_id)
        published_archive = progress.get("PUBLISHED_ARCHIVE", 0)
        published_briefing = progress.get("PUBLISHED_BRIEFING", 0)
        
        total = sum(progress.values())
        skipped = progress.get("SKIPPED", 0)
        items_pending = total - published_archive - skipped

        return ResumeReport(
            tool_name="publisher",
            resumable=batch_info["status"] in ("PENDING", "PARTIAL", "PUBLISHING"),
            items_completed=published_archive,
            items_pending=items_pending,
            message=f"Resuming Publisher. {published_archive} fully published, {items_pending} pending/failed.",
            details={"progress": progress, "batch_status": batch_info["status"], "total": total},
        )
