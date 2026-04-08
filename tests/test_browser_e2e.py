# tests/test_browser_e2e.py
import time
import os
import json
import pytest
from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

def test_wikipedia_summary():
    headers = {"X-API-Key": "dev_default_key_change_me_in_production"}
    payload = {
        "tool_name": "browser_operator",
        "args": {
            "target": "https://google.com",
            "task": "summarize PHM film plot"
        },
        "client_metadata": {"enable_tracker": True}
    }
    
    response = client.post("/api/tools/browser_operator", json=payload, headers=headers)
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    
    status = "QUEUED"
    max_attempts = 30
    for i in range(max_attempts):
        status_res = client.get(f"/api/jobs/{job_id}", headers=headers)
        if status_res.status_code == 200:
            status = status_res.json()["status"]
            if status in ("COMPLETED", "FAILED", "CANCELLED"):
                break
        time.sleep(min(2 ** (i/2), 5))  # Exponential backoff capped at 5s
        
    assert status == "COMPLETED"
    
    ledger_path = f"artifacts/test_runs/ledger_{job_id}.json"
    assert os.path.exists(ledger_path)
    
    with open(ledger_path, "r") as f:
        ledger = json.load(f)
        assert len(ledger["steps"]) > 0
        assert any(step["action"] == "MILESTONE: Initial Load" for step in ledger["steps"]) 
        assert any(step["action"] == "MILESTONE: Success Declaration" for step in ledger["steps"])
