# utils/tracker.py
import os
import json
from datetime import datetime, timezone

class TestTracker:
    def __init__(self, job_id: str, enabled: bool):
        self.job_id = job_id
        self.enabled = enabled
        self.ledger_path = f"artifacts/test_runs/ledger_{job_id}.json"
        if self.enabled:
            os.makedirs("artifacts/test_runs", exist_ok=True)
            with open(self.ledger_path, "w", encoding="utf-8") as f:
                json.dump({"job_id": job_id, "steps": []}, f, indent=2)
                f.flush()

    def log_step(self, action: str, mode: str, html_snippet: str = ""):
        if not self.enabled:
            return
        # Use read/write append-safe pattern
        try:
            with open(self.ledger_path, "r+", encoding="utf-8") as f:
                data = json.load(f)
                data["steps"].append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "action": action,
                    "mode": mode,
                    "html_snippet": html_snippet,
                })
                f.seek(0)
                json.dump(data, f, indent=2)
                f.truncate()
        except FileNotFoundError:
            # Recreate if missing
            with open(self.ledger_path, "w", encoding="utf-8") as f:
                json.dump({"job_id": self.job_id, "steps": [{
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "action": action,
                    "mode": mode,
                    "html_snippet": html_snippet,
                }]}, f, indent=2)
        except Exception:
            # Logging must not crash the caller
            return

    def capture_milestone(self, name: str, full_html: str):
        if not self.enabled:
            return
        self.log_step(f"MILESTONE: {name}", "System", full_html)
