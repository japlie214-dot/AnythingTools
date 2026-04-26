# tools/backup/models.py
from pydantic import BaseModel, Field
from typing import Optional, Dict

class Watermark(BaseModel):
    last_article_id: str = Field(default="", description="Last exported article ULID")
    last_export_ts: Optional[str] = Field(default=None, description="Timestamp of last successful export")
    total_articles_exported: int = Field(default=0, description="Cumulative count of exported articles")
    total_vectors_exported: int = Field(default=0, description="Cumulative count of exported vectors")
    table_watermarks: Dict[str, str] = Field(default_factory=dict, description="Per-table last-export timestamps")

    def model_dump_compat(self) -> dict:
        """Return dict representation, compatible with both Pydantic v1 and v2."""
        if hasattr(self, "model_dump"):
            return self.model_dump()
        return self.dict()

class BackupStatusResponse(BaseModel):
    enabled: bool
    backup_dir: str
    watermark: Watermark
    total_size_bytes: int
    table_counts: Dict[str, int] = Field(default_factory=dict)

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
