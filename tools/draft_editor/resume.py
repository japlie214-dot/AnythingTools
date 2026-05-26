# tools/draft_editor/resume.py
from tools.base import BaseResumeHandler, ResumeReport

class ResumeHandler(BaseResumeHandler):
    def check_resume_state(self) -> ResumeReport:
        return ResumeReport(
            tool_name="draft_editor",
            resumable=False,
            items_completed=0,
            items_pending=0,
            message="Draft Editor does not support resumption. Transactions are atomic.",
            details={
                "tool": "draft_editor",
                "suggestion": "Re-run draft_editor with corrected operations. Changes are only committed on success."
            },
        )
