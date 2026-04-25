# api/schemas.py
from pydantic import BaseModel
from typing import Any, Dict, List, Optional


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


class BackupStatusResponse(BaseModel):
    enabled: bool
    backup_dir: str
    watermark: WatermarkSchema
    article_files: int
    vector_files: int
    total_size_bytes: int


class ExportQueuedResponse(BaseModel):
    status: str = "EXPORT_QUEUED"
    message: str


class RestoreQueuedResponse(BaseModel):
    status: str = "RESTORE_QUEUED"
    message: str
