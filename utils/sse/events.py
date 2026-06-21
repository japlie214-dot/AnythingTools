# utils/sse/events.py
"""Pydantic models for SSE event payloads.

Each model serializes to a JSON string via model_dump_json(), which is the
recommended wire-serialization method per Pydantic v2 docs:
https://docs.pydantic.dev/latest/api/base_model/#pydantic.BaseModel.model_dump_json

The SSE wire format requires UTF-8 encoding (HTML spec §9.2):
https://html.spec.whatwg.org/multipage/server-sent-events.html
model_dump_json() defaults to ensure_ascii=False, matching this requirement.
"""

from __future__ import annotations
from typing import Any, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SSEEvent(BaseModel):
    """Base SSE event. All events carry a timestamp and event_type."""
    timestamp: str = Field(default_factory=_now_iso)
    event_type: str

    def to_sse_data(self) -> str:
        """Serialize to JSON string for SSE ``data:`` field."""
        return self.model_dump_json()


class JobStatusEvent(SSEEvent):
    """Emitted when a job's status changes (QUEUED → RUNNING → COMPLETED, etc.)."""
    event_type: str = "job.status_changed"
    job_id: str
    status: str
    tool_name: Optional[str] = None


class LogEntryEvent(SSEEvent):
    """Emitted for each new log entry appended to logs.db for this job."""
    event_type: str = "log.appended"
    job_id: str
    level: str
    tag: str
    message: str
    payload: Optional[dict[str, Any]] = None
    log_timestamp: str


class ToolProgressEvent(SSEEvent):
    """Emitted when a tool reports intermediate progress via telemetry."""
    event_type: str = "tool.progress"
    job_id: str
    tool_name: str
    message: str
    status: str = "RUNNING"
    payload: Optional[dict[str, Any]] = None


class ToolCompletedEvent(SSEEvent):
    """Emitted when a tool finishes successfully."""
    event_type: str = "tool.completed"
    job_id: str
    tool_name: str
    output: str
    attachment_paths: list[str] = Field(default_factory=list)


class JobFailedEvent(SSEEvent):
    """Emitted when a job reaches FAILED or ABANDONED state.

    The error field contains the untruncated error message and traceback
    so the client can pinpoint the failure without querying logs.db.
    """
    event_type: str = "job.failed"
    job_id: str
    tool_name: Optional[str] = None
    error: str
    traceback: Optional[str] = None


class StreamEndEvent(SSEEvent):
    """Sentinel event signaling the SSE stream is closing."""
    event_type: str = "stream.end"
    job_id: str
    reason: str  # "terminal_state" | "client_disconnect" | "timeout" | "server_shutdown"
