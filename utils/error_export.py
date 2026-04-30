# utils/error_export.py
import json
from datetime import datetime, timezone
from pathlib import Path
from utils.logger.routing import _LOG_DIR
from utils.logger.state import _tool_log_buffer

def export_error_context_enhanced(error_tag: str, error_message: str, job_id: str | None, event_id: str) -> Path | None:
    """Export last 50 logs from logs.db based on event_id, non-blocking."""
    import re
    import sqlite3
    from database.connection import LogsDatabaseManager
    
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_tag = re.sub(r'[<>:"/\\|?*]', '_', error_tag).replace(" ", "_")[:50]
        filename = f"error_{timestamp}_{safe_tag}.txt"
        filepath = _LOG_DIR / filename
        
        db_entries = []
        try:
            conn = LogsDatabaseManager.get_read_connection()
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT timestamp FROM logs WHERE event_id = ?", (event_id,)).fetchone()
            if row:
                error_ts = row["timestamp"]
                rows = conn.execute(
                    "SELECT timestamp, level, tag, message, payload_json FROM logs WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 50",
                    (error_ts,)
                ).fetchall()
                for r in reversed(rows):
                    entry = dict(r)
                    if entry.get("payload_json"):
                        try:
                            entry["payload"] = json.loads(entry["payload_json"])
                        except Exception:
                            entry["payload"] = entry["payload_json"]
                    db_entries.append(entry)
        except Exception:
            pass
        
        if not db_entries:
            buf = _tool_log_buffer.get()
            db_entries = buf[-50:] if buf else []
        
        if not db_entries:
            return None
        
        lines = [
            "=" * 80,
            f"ERROR EXPORT - {timestamp}",
            "=" * 80,
            f"Tag: {error_tag}",
            f"Message: {error_message}",
            f"Job ID: {job_id or 'N/A'}",
            f"Event ID: {event_id}",
            "-" * 80,
            "LOG ENTRIES (last 50):",
            "-" * 80,
        ]
        
        for entry in db_entries:
            ts = entry.get("timestamp", "")
            lvl = entry.get("level", "")
            t = entry.get("tag", "")
            msg = entry.get("message", "")
            payload = entry.get("payload")
            
            lines.append(f"[{ts}] [{lvl}] [{t}] {msg}")
            if payload:
                try:
                    payload_str = json.dumps(payload, default=str, ensure_ascii=False) if not isinstance(payload, str) else payload
                    lines.append(f"  Payload: {payload_str}")
                except Exception:
                    lines.append("  Payload: <unserializable>")
        
        lines.append("=" * 80)
        filepath.write_text("\n".join(lines), encoding="utf-8")
        return filepath
    except Exception:
        return None
