# tools/stock_notes/resume.py
from tools.base import BaseResumeHandler, ResumeReport
from database.connection import DatabaseManager

class StockNotesResumeHandler(BaseResumeHandler):
    def check_resume_state(self) -> ResumeReport:
        conn = DatabaseManager.get_read_connection()
        cursor = conn.execute("SELECT status, COUNT(*) FROM job_items WHERE job_id = ? GROUP BY status", (self.job_id,))
        counts = dict(cursor.fetchall())
        
        completed = counts.get("COMPLETED", 0)
        pending_failed = counts.get("PENDING", 0) + counts.get("FAILED", 0) + counts.get("RUNNING", 0)
        
        return ResumeReport(
            tool_name="stock_notes",
            resumable=(pending_failed > 0 and completed > 0),
            items_completed=completed,
            items_pending=pending_failed,
            message=f"Stock Notes extraction interrupted. {completed} complete, {pending_failed} pending.",
        )
