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
