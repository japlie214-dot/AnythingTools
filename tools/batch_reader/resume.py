# tools/batch_reader/resume.py
from tools.base import BaseResumeHandler, ResumeReport

class ResumeHandler(BaseResumeHandler):
    def check_resume_state(self) -> ResumeReport:
        return ResumeReport(
            tool_name="batch_reader",
            resumable=False,
            items_completed=0,
            items_pending=0,
            message="Batch Reader does not support resumption.",
            details=None,
        )
