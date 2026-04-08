from pydantic import BaseModel
from typing import Any, Dict, List, Optional


class JobCreateRequest(BaseModel):
    tool_name: str
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
