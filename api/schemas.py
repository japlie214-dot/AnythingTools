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


class ResumeResponse(BaseModel):
    job_id: str
    tool_name: str
    status: str
    items_completed: int
    items_pending: int
    message: str
    details: Optional[Dict[str, Any]] = None


class EngineMetrics(BaseModel):
    status: str
    error: Optional[str] = None


class SyncMetrics(BaseModel):
    pending_conflicts: int = 0
    dead_letter_count: int = 0
    last_sync_time: Optional[str] = None
    cloud_writer_stats: Optional[Dict[str, int]] = None


class BackupMetricsResponse(BaseModel):
    local_engine: EngineMetrics
    cloud_engine: EngineMetrics
    sync_status: SyncMetrics
    circuit_breaker_state: str
