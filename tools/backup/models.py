# tools/backup/models.py
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field

class Watermark(BaseModel):
    last_article_id: str = Field(default="", description="Last exported article ULID")
    last_export_ts: Optional[str] = Field(default=None, description="Timestamp of last successful export")
    total_articles_exported: int = Field(default=0, description="Cumulative count of exported articles")
    total_vectors_exported: int = Field(default=0, description="Cumulative count of exported vectors")

class BackupStatusResponse(BaseModel):
    enabled: bool
    backup_dir: str
    watermark: Watermark
    article_files: int
    vector_files: int
    total_size_bytes: int

class ExportResult(BaseModel):
    success: bool
    articles_exported: int
    vectors_exported: int
    article_file: Optional[str] = None
    vector_file: Optional[str] = None
    new_watermark: str
    duration_seconds: float
    error: Optional[str] = None

class RestoreResult(BaseModel):
    success: bool
    articles_restored: int
    vectors_restored: int
    files_processed: int
    duration_seconds: float
    error: Optional[str] = None
