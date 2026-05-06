# tools/publisher/resume.py
import json
from typing import Any
from tools.base import BaseResumeHandler, ResumeReport
from database.connection import DatabaseManager

class ResumeHandler(BaseResumeHandler):
    def check_resume_state(self) -> ResumeReport:
        batch_id = self.args.get("batch_id")
        if not batch_id:
            return ResumeReport("publisher", False, 0, 0, "No batch_id provided in arguments.")
            
        conn = DatabaseManager.get_read_connection()
        row = conn.execute("SELECT phase_state FROM broadcast_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        
        if not row or not row["phase_state"]:
            return ResumeReport("publisher", False, 0, 0, "Batch ledger not found.")
            
        state = json.loads(row["phase_state"])
        
        briefing_done = sum(1 for v in state.get("publish_briefing", {}).values() if v.get("status") == "COMPLETED")
        archive_done = sum(1 for v in state.get("publish_archive", {}).values() if v.get("status") == "COMPLETED")
        completed = briefing_done + archive_done
        
        all_ulids = set(state.get("validate", {}).keys())
        pending = max(0, (len(all_ulids) * 2) - completed)
        
        return ResumeReport(
            tool_name="publisher",
            resumable=True,
            items_completed=completed,
            items_pending=pending,
            message=f"Resuming Publisher. {completed} steps completed, {pending} pending.",
            details=state,
        )
