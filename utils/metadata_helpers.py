"""utils/metadata_helpers.py

Shared utilities for creating and parsing item_metadata JSON.
"""
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

STEP_TRANSLATE = "translate"
STEP_PUBLISH_BRIEFING = "publish_briefing"
STEP_PUBLISH_ARCHIVE = "publish_archive"


def make_metadata(
    step_type: str,
    ulid: str,
    retry: int = 0,
    model: Optional[str] = None,
    error: Optional[str] = None,
    is_top10: bool = False,
    **extra: Any
) -> str:
    meta = {
        "step": step_type,
        "ulid": ulid,
        "retry": retry,
        "timestamp": _utcnow(),
    }
    if model:
        meta["model"] = model
    if error:
        meta["error"] = error
    if is_top10:
        meta["is_top10"] = True
    if extra:
        meta.update(extra)
    return json.dumps(meta, ensure_ascii=False)


def parse_metadata(metadata_json: str) -> Dict[str, Any]:
    if not metadata_json:
        return {}
    try:
        data = json.loads(metadata_json)
        # Ensure expected keys have defaults without stripping arbitrary **extra keys
        data.setdefault("step", "unknown")
        data.setdefault("ulid", "")
        data.setdefault("retry", 0)
        data.setdefault("timestamp", "")
        data.setdefault("is_top10", False)
        return data
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid metadata JSON: {e}")


def increment_retry(metadata_json: str) -> str:
    parsed = parse_metadata(metadata_json) if metadata_json else {}
    parsed["retry"] = parsed.get("retry", 0) + 1
    parsed["timestamp"] = _utcnow()
    return json.dumps(parsed, ensure_ascii=False)


def add_error(metadata_json: str, error_msg: str) -> str:
    parsed = parse_metadata(metadata_json) if metadata_json else {}
    parsed["error"] = error_msg
    parsed["timestamp"] = _utcnow()
    return json.dumps(parsed, ensure_ascii=False)
