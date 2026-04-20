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
