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

        batch_status = batch_info.get("status", "UNKNOWN")
        if batch_status == "COMPLETED":
            return ResumeReport(tool_name="publisher", resumable=False, items_completed=batch_info.get("article_count", 0), items_pending=0, message=f"Batch {batch_id} already fully published.")

        progress = get_batch_publish_progress(batch_id)
        published_archive = progress.get("PUBLISHED_ARCHIVE", 0)
        published_briefing = progress.get("PUBLISHED_BRIEFING", 0)
        skipped = progress.get("SKIPPED", 0)
        failed = progress.get("FAILED", 0)
        
        total = sum(progress.values())
        items_completed = published_archive + published_briefing + skipped
        items_pending = total - items_completed
        
        resumable_states = ("PENDING", "PARTIAL", "PUBLISHING")
        is_resumable = batch_status in resumable_states and items_pending > 0
        
        message = f"Batch {batch_id} has {items_completed} completed, {items_pending} pending."
        if failed > 0 and batch_status != "PUBLISHING":
            is_resumable = True
            message += f" Contains {failed} failed items that will be retried."
            
        return ResumeReport(
            tool_name="publisher",
            resumable=is_resumable,
            items_completed=items_completed,
            items_pending=items_pending,
            message=message,
            details={"progress": progress, "batch_status": batch_status, "total": total, "failed": failed},
        )
