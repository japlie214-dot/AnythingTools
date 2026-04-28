# utils/error_export.py
import json
from datetime import datetime, timezone
from pathlib import Path
from utils.logger.routing import _LOG_DIR
from utils.logger.state import _tool_log_buffer

def export_error_context(error_tag: str, error_message: str, job_id: str | None = None) -> Path | None:
    """Export the current tool's in-memory log buffer to a text file on error."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        buf = _tool_log_buffer.get()
        if not buf:
            return None
        
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        safe_tag = error_tag.replace(":", "_").replace(" ", "_")[:50]
        filename = f"error_{timestamp}_{safe_tag}.txt"
        filepath = _LOG_DIR / filename
        
        lines = [
            "=" * 80,
            f"ERROR EXPORT - {timestamp}",
            "=" * 80,
            f"Tag: {error_tag}",
            f"Message: {error_message}",
            f"Job ID: {job_id or 'N/A'}",
            "-" * 80,
            "LOG BUFFER CONTENTS:",
            "-" * 80,
        ]
        
        for entry in buf:
            ts = entry.get("timestamp", "")
            lvl = entry.get("level", "")
            t = entry.get("tag", "")
            msg = entry.get("message", "")
            payload = entry.get("payload")
            
            lines.append(f"[{ts}] [{lvl}] [{t}] {msg}")
            if payload:
                try:
                    payload_str = json.dumps(payload, default=str, ensure_ascii=False)
                    lines.append(f"  Payload: {payload_str}")
                except Exception:
                    lines.append("  Payload: <unserializable>")
        
        lines.append("=" * 80)
        
        filepath.write_text("\n".join(lines), encoding="utf-8")
        return filepath
    except Exception:
        return None
