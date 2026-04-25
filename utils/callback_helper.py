# utils/callback_helper.py

import json
import os
import sqlite3
import config
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

class CallbackStatus(str, Enum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    PENDING_CALLBACK = "PENDING_CALLBACK"
    CANCELLING = "CANCELLING"

@dataclass(frozen=True)
class StatusDefinition:
    description: str
    next_steps: str
    rerunnable: bool = False

STATUS_DEFINITIONS: Dict[str, StatusDefinition] = {
    CallbackStatus.COMPLETED.value: StatusDefinition(
        description="Job finished successfully. All operations completed as expected.",
        next_steps="No action required. Review the results and artifact inventory below.",
        rerunnable=False,
    ),
    CallbackStatus.PARTIAL.value: StatusDefinition(
        description="Job completed with partial success. Some operations failed but others succeeded.",
        next_steps="You may retry by submitting the same job. Completed items will be skipped.",
        rerunnable=True,
    ),
    CallbackStatus.FAILED.value: StatusDefinition(
        description="Job encountered a fatal error.",
        next_steps="Review the error details below, fix the underlying issue, and resubmit.",
        rerunnable=True,
    ),
    CallbackStatus.PENDING_CALLBACK.value: StatusDefinition(
        description="Job processing completed but the result delivery failed. Automatic retry is scheduled.",
        next_steps="Wait for the system to retry delivery.",
        rerunnable=False,
    ),
    CallbackStatus.CANCELLING.value: StatusDefinition(
        description="Cancellation was requested. The job will stop at the next safe checkpoint.",
        next_steps="Wait for the job to reach a checkpoint and fully stop.",
        rerunnable=True,
    ),
}

def generate_header(job_id: str, status: str, tool_name: str, timestamp: Optional[str] = None) -> str:
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    emoji = {"COMPLETED": "[OK]", "PARTIAL": "[PARTIAL]", "FAILED": "[FAILED]"}.get(status.upper(), "[INFO]")
    return f"""# {emoji} AnythingTools Job Report

| Field | Value |
|-------|-------|
| **Job ID** | `{job_id}` |
| **Tool** | `{tool_name}` |
| **Status** | `{status.upper()}` |
| **Timestamp** | `{ts}` |

---

"""

def format_artifacts_list(artifacts: List[Dict[str, Any]], artifacts_subdir: Optional[str] = None) -> str:
    if not artifacts:
        return "### Artifacts\n\n_No artifacts produced for this job._\n\n---\n\n"
    lines = ["### Artifacts\n"]
    if artifacts_subdir:
        # Render absolute artifact directory path for AnythingLLM consumers
        subdir_path = Path(str(artifacts_subdir))
        base_dir = getattr(config, "ANYTHINGLLM_ARTIFACTS_DIR", None)
        if base_dir and not subdir_path.is_absolute():
            full_path = Path(base_dir) / str(artifacts_subdir).strip("/")
            full_path = full_path.as_posix()
        else:
            full_path = subdir_path.as_posix()
        lines.append(f"> **Artifacts Directory:** `{full_path}`\n")
    lines.extend(["| # | Filename | Type | Description |", "|---|---|----------|------|-------------|"])
    for i, art in enumerate(artifacts, 1):
        lines.append(f"| {i} | `{art.get('filename', 'unknown')}` | {art.get('type', 'file')} | {art.get('description', '')} |")
    return "\n".join(lines) + "\n\n---\n"

def _fetch_recent_errors(job_id: str, limit: int = 10) -> str:
    if not job_id:
        return ""
    try:
        from database.connection import DatabaseManager
        conn = DatabaseManager.get_read_connection()
        rows = conn.execute(
            "SELECT timestamp, tag, level, message FROM job_logs "
            "WHERE job_id = ? AND level IN ('ERROR', 'WARNING', 'CRITICAL') "
            "ORDER BY timestamp DESC LIMIT ?",
            (job_id, limit)
        ).fetchall()
        if not rows:
            return ""
        lines = ["### Recent Errors & Warnings", ""]
        for r in rows:
            lines.append(f"- `{r['timestamp']}` | `{r['level']}` | `{r['tag']}` | {r['message']}")
        return "\n".join(lines) + "\n\n"
    except Exception:
        return ""

def inject_status_definitions(status: str, overrides: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    local_defs = dict(STATUS_DEFINITIONS)
    if overrides:
        for s_name, s_def in overrides.items():
            local_defs[s_name.upper()] = StatusDefinition(
                description=s_def.get("description", ""),
                next_steps=s_def.get("next_steps", ""),
                rerunnable=s_def.get("rerunnable", False)
            )
    
    definition = local_defs.get(status.upper())
    if not definition:
        return ""
        
    return f"""### Status & Next Steps

**Current Status: `{status.upper()}`**

- **Description:** {definition.description}
- **Can be retried:** {'Yes' if definition.rerunnable else 'No'}
- **Next Steps:** {definition.next_steps}

---
"""

def format_callback_message(
    job_id: str, status: str, tool_name: str, summary: str,
    details: Optional[Dict[str, Any]] = None, artifacts: Optional[List[Dict[str, Any]]] = None,
    artifacts_subdir: Optional[str] = None, timestamp: Optional[str] = None,
    status_overrides: Optional[Dict[str, Dict[str, Any]]] = None
) -> str:
    sections = [generate_header(job_id, status, tool_name, timestamp)]

    if status.upper() == "FAILED":
        summary = summary + "\n\n" + _fetch_recent_errors(job_id)

    if summary:
        sections.append(f"### Summary\n\n{summary}\n\n---\n")

    sections.append(format_artifacts_list(artifacts or [], artifacts_subdir))
    sections.append(inject_status_definitions(status, status_overrides))
    return "".join(sections)

def truncate_message(message: str, max_chars: int = 12000) -> str:
    base_limit = getattr(config, "LLM_CONTEXT_CHAR_LIMIT", 40000)
    multiplier = getattr(config, "CALLBACK_TRUNCATION_MULTIPLIER", 0.5)
    dynamic_limit = int(base_limit * multiplier)
    
    # Ignore the hardcoded max_chars legacy parameter to truly enforce the dynamic context budget
    effective_limit = dynamic_limit

    if len(message) <= effective_limit:
        return message
    return message[:effective_limit - 100] + f"\n\n[Message truncated to {effective_limit} chars. See full result in job logs.]"
