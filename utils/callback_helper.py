# utils/callback_helper.py

import json
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

def format_artifacts_list(artifacts: List[Dict[str, Any]], artifacts_dir: Optional[str] = None) -> str:
    if not artifacts:
        return "### Artifacts\n\n_No artifacts produced for this job._\n\n---\n\n"
    lines = ["### Artifacts\n"]
    if artifacts_dir:
        lines.append(f"> **Artifacts Directory:** `{artifacts_dir}`\n")
    lines.extend(["| # | Filename | Type | Description |", "|---|----------|------|-------------|"])
    for i, art in enumerate(artifacts, 1):
        lines.append(f"| {i} | `{art.get('filename', 'unknown')}` | {art.get('type', 'file')} | {art.get('description', '')} |")
    return "\n".join(lines) + "\n\n---\n"

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
    artifacts_dir: Optional[str] = None, timestamp: Optional[str] = None,
    status_overrides: Optional[Dict[str, Dict[str, Any]]] = None
) -> str:
    sections = [
        generate_header(job_id, status, tool_name, timestamp),
        f"### Summary\n\n{summary}\n\n---\n"
    ]
    if details:
        sections.append(f"### Details\n\n```json\n{json.dumps(details, indent=2, ensure_ascii=False)}\n```\n\n---\n")
    sections.append(format_artifacts_list(artifacts or [], artifacts_dir))
    sections.append(inject_status_definitions(status, status_overrides))
    return "".join(sections)

def truncate_message(message: str, max_chars: int = 12000) -> str:
    if len(message) <= max_chars:
        return message
    return message[:max_chars - 100] + f"\n\n[Message truncated to {max_chars} chars. See full result in job logs.]"
