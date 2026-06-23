# api/schemas.py
"""Pydantic schemas for the agent-native sync execution API.

The sync model: POST /api/jobs holds the HTTP connection open until the
job reaches a terminal state (COMPLETED, FAILED, ABANDONED, PARTIAL, SKIPPED)
or PAUSED_FOR_HITL. The response body IS the terminal state.
"""
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional, Literal
from enum import Enum


class ResumeRequest(BaseModel):
    """Request body for POST /api/jobs/{id}/resume.

    `decision` is only meaningful for PAUSED_FOR_HITL jobs (delivered to the
    worker via HitlResolutionRegistry). Ignored for INTERRUPTED/FAILED/PARTIAL.
    """
    decision: Literal["proceed", "skip", "cancel"] = "proceed"


class SyncJobRequest(BaseModel):
    """Request body for POST /api/jobs."""
    tool_name: str = Field(..., description="The tool to execute.")
    args: Dict[str, Any] = Field(default_factory=dict, description="Tool input arguments.")
    client_metadata: Optional[Dict[str, Any]] = Field(None, description="Optional metadata (idempotency key, etc.).")


class SyncJobResponse(BaseModel):
    """Response for POST /api/jobs and POST /api/jobs/{id}/resume.

    The `status` field is the terminal state (or PAUSED_FOR_HITL).
    `result` is the tool's output (any JSON-serializable).
    `error` is the full, untruncated exception message if status is FAILED.
    `logs_pointer` tells the LLM where to query for more context.
    """
    job_id: str
    status: str
    result: Optional[Any] = None
    error: Optional[str] = None
    tool_name: Optional[str] = None
    logs_pointer: Optional[str] = None
    hitl_url: Optional[str] = None
    hitl_reason: Optional[str] = None


# Legacy schemas — kept for backward compat with POST /api/tools/{tool_name}
class JobCreateRequest(BaseModel):
    args: Dict[str, Any] = {}
    client_metadata: Optional[Dict[str, Any]] = None


class JobCreateResponse(BaseModel):
    """Deprecated: use SyncJobResponse via POST /api/jobs."""
    job_id: str
    status: str


class JobLogEntry(BaseModel):
    timestamp: str
    level: str
    tag: Optional[str] = None
    status_state: Optional[str] = None
    message: str


class JobStatusResponse(BaseModel):
    """Response for GET /api/jobs/{id}/status."""
    job_id: str
    status: str
    job_logs: List[JobLogEntry]
    final_payload: Optional[Dict[str, Any]] = None


class ResumeResponse(BaseModel):
    """Deprecated: POST /api/jobs/{id}/resume now returns SyncJobResponse."""
    job_id: str
    tool_name: str
    status: str
    items_completed: int
    items_pending: int
    message: str
    details: Optional[Dict[str, Any]] = None


class HealthCheckRequest(BaseModel):
    pass


class HealthCheckResponse(BaseModel):
    """Response for POST /api/health-check/{tool_name}.

    No longer returns stream_url — the sync API returns the full result.
    """
    job_id: str
    tool_name: str
    timeout_seconds: int
    final_result: Optional[SyncJobResponse] = None


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
