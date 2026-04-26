# api/schemas.py
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional
from enum import Enum


class JobCreateRequest(BaseModel):
    args: Dict[str, Any] = {}
    client_metadata: Optional[Dict[str, Any]] = None


class JobCreateResponse(BaseModel):
    job_id: str
    status: str


class JobLogEntry(BaseModel):
    timestamp: str
    level: str
    tag: Optional[str] = None
    status_state: Optional[str] = None
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    job_logs: List[JobLogEntry]
    final_payload: Optional[Dict[str, Any]] = None


class WatermarkSchema(BaseModel):
    last_article_id: str = ""
    last_export_ts: Optional[str] = None
    total_articles_exported: int = 0
    total_vectors_exported: int = 0


class BackupMode(str, Enum):
    FULL = "full"
    DELTA = "delta"


class BackupStatusResponse(BaseModel):
    enabled: bool
    backup_dir: str
    watermark: WatermarkSchema
    file_counts: Dict[str, int] = Field(default_factory=dict)
    total_size_bytes: int


class ExportQueuedResponse(BaseModel):
    status: str = "EXPORT_QUEUED"
    message: str
    job_id: Optional[str] = None


class RestoreQueuedResponse(BaseModel):
    status: str = "RESTORE_QUEUED"
    message: str
    job_id: Optional[str] = None
