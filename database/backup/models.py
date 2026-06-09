# database/backup/models.py
from pydantic import BaseModel, Field
from typing import Optional, Dict


class ExportResult(BaseModel):
    success: bool
    exported_counts: Dict[str, int] = Field(default_factory=dict)
    new_watermark: str = ""
    duration_seconds: float = 0.0
    error: Optional[str] = None

class RestoreResult(BaseModel):
    success: bool
    restored_counts: Dict[str, int] = Field(default_factory=dict)
    duration_seconds: float = 0.0
    error: Optional[str] = None

from dataclasses import dataclass

@dataclass
class SyncDecision:
    action: str  # "push_only" | "bidirectional" | "pull_only" | "skip" | "abort"
    reason: str
    local_proofs: Dict[str, Dict]
    cloud_proofs: Dict[str, Dict]
    divergence_detected: bool = False
    hitl_required: bool = False
    hitl_outcome: Optional[str] = None
    recommended_strategy: str = "operational_wins"
    duration_seconds: float = 0.0

class Watermark(BaseModel):
    last_article_id: str = ""
    total_articles_exported: int = 0
    table_watermarks: Dict[str, str] = Field(default_factory=dict)

    def model_dump_compat(self) -> dict:
        data = self.model_dump()
        data["table_watermarks"] = self.table_watermarks
        return data
