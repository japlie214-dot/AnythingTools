# api/sse/decoder.py
"""Decode tool _callback_format: structured payloads into SSE completed data.

Tools emit JSON like:
  {"_callback_format": "structured", "tool_name": "scraper",
   "status": "COMPLETED", "summary": "...", "details": {...},
   "artifacts": [...], "status_overrides": {...}}

The SSE completed event surfaces these as separate fields. Enforces a size
budget on `details` to prevent multi-MB payloads from blocking the event
loop — replaces the deleted truncate_message() from utils/callback_helper.py.
"""
import json
from typing import Any

try:
    import config
    _DETAILS_LIMIT = int(getattr(config, "LLM_CONTEXT_CHAR_LIMIT", 800000) *
                         getattr(config, "CALLBACK_TRUNCATION_MULTIPLIER", 0.5))
except Exception:
    _DETAILS_LIMIT = 400000


def decode_tool_result(result_json: str | None) -> dict[str, Any]:
    """Decode a job's result_json into SSE completed event data.

    Returns a dict with keys: summary, details, artifacts, status_overrides,
    status, raw. Never raises — on any decode error, returns {raw: result_json}.
    """
    if not result_json:
        return {"summary": "", "details": None, "artifacts": [], "status_overrides": None, "status": "UNKNOWN", "raw": None}
    try:
        parsed = json.loads(result_json)
    except Exception:
        return {"summary": "", "details": None, "artifacts": [], "status_overrides": None, "status": "UNKNOWN", "raw": result_json}

    # Worker wraps tool output in {"status": ..., "result": <tool_output>}.
    # See bot/engine/worker.py:337,339,341.
    inner = parsed.get("result", parsed) if isinstance(parsed, dict) else parsed

    if isinstance(inner, dict) and inner.get("_callback_format") == "structured":
        details = inner.get("details")
        # Enforce size budget. Ref: original truncate_message at
        # utils/callback_helper.py:169-179 (deleted in this refactor).
        if details is not None:
            try:
                details_str = json.dumps(details, ensure_ascii=False, default=str)
                if len(details_str) > _DETAILS_LIMIT:
                    details = {"_truncated": True, "preview": details_str[:_DETAILS_LIMIT - 200]}
            except Exception:
                details = {"_decode_error": True}
        return {
            "summary": inner.get("summary", ""),
            "details": details,
            "artifacts": inner.get("artifacts") or [],
            "status_overrides": inner.get("status_overrides"),
            "status": inner.get("status", parsed.get("status", "UNKNOWN")),
            "raw": None,
        }
    # Non-structured: return as raw.
    return {
        "summary": str(inner)[:500] if inner is not None else "",
        "details": None,
        "artifacts": parsed.get("attachment_paths", []) if isinstance(parsed, dict) else [],
        "status_overrides": None,
        "status": parsed.get("status", "UNKNOWN") if isinstance(parsed, dict) else "UNKNOWN",
        "raw": inner if not isinstance(inner, dict) else None,
    }
