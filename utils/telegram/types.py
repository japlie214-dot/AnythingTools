# utils/telegram/types.py
from dataclasses import dataclass, field
from typing import Dict, Any

@dataclass
class TelegramErrorInfo:
    success: bool
    is_permanent: bool = False
    is_transient: bool = False
    description: str = ""
    retry_after: int = 0

@dataclass
class PhaseState:
    validate: Dict[str, Dict[str, str]] = field(default_factory=dict)
    translate: Dict[str, Dict[str, str]] = field(default_factory=dict)
    publish_briefing: Dict[str, Dict[str, str]] = field(default_factory=dict)
    publish_archive: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def is_completed(self, phase: str, ulid: str) -> bool:
        return getattr(self, phase, {}).get(ulid, {}).get("status") == "COMPLETED"

    def mark_completed(self, phase: str, ulid: str) -> None:
        getattr(self, phase)[ulid] = {"status": "COMPLETED"}

    def mark_failed(self, phase: str, ulid: str) -> None:
        getattr(self, phase)[ulid] = {"status": "FAILED"}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "validate": self.validate,
            "translate": self.translate,
            "publish_briefing": self.publish_briefing,
            "publish_archive": self.publish_archive,
        }

@dataclass
class PublishCounter:
    batch_id: str
    total_articles: int = 0
    total_briefing: int = 0
    total_archive: int = 0
    briefing_sent: int = 0
    briefing_failed: int = 0
    archive_sent: int = 0
    archive_failed: int = 0
    messages_sent: int = 0

    def increment(self, phase: str, success: bool = True, message_count: int = 1) -> None:
        self.messages_sent += message_count
        if phase == "briefing":
            if success:
                self.briefing_sent += 1
            else:
                self.briefing_failed += 1
        elif phase == "archive":
            if success:
                self.archive_sent += 1
            else:
                self.archive_failed += 1

    def snapshot(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "total_articles": self.total_articles,
            "total_briefing": self.total_briefing,
            "total_archive": self.total_archive,
            "briefing_sent": self.briefing_sent,
            "briefing_failed": self.briefing_failed,
            "briefing_progress": f"{self.briefing_sent}/{self.total_briefing}" if self.total_briefing > 0 else "N/A",
            "archive_sent": self.archive_sent,
            "archive_failed": self.archive_failed,
            "archive_progress": f"{self.archive_sent}/{self.total_archive}" if self.total_archive > 0 else "N/A",
            "messages_sent": self.messages_sent,
        }

    @property
    def total_processed(self) -> int:
        return self.briefing_sent + self.briefing_failed + self.archive_sent + self.archive_failed
