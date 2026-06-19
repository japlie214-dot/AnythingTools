# tests/test_browser_e2e.py
"""End-to-end validation of the scraper tool's browser-orchestrator loop.

This test enqueues a real scraper job via the FastAPI TestClient, polls for
completion, and validates the execution ledger. It requires:
  1. A Chrome/Chromium binary on PATH (skipped via skipif if absent).
  2. Network access to the target URL (skipped via the `network` marker).
  3. The DATABASE_INTEGRATION_ENABLED autouse fixture in conftest.py is
     inherited — but this test needs the DB ON to track the job. Override
     via the ENV var directly if needed.

References:
- pytest skipif: https://docs.pytest.org/en/stable/how-to/skipping.html
- FastAPI TestClient: https://fastapi.tiangolo.com/reference/testclient/
"""
import json
import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Skip the entire module if no Chrome/Chromium binary is on PATH.
# Per pytest docs: "A skip means that you expect your test to pass only if
# some conditions are met... skipping tests that depend on an external
# resource which is not available at the moment."
# https://docs.pytest.org/en/stable/how-to/skipping.html
_CHROMIUM_BINARY = shutil.which("chromium") or shutil.which("chrome") or shutil.which("google-chrome")
pytestmark = pytest.mark.skipif(
    _CHROMIUM_BINARY is None,
    reason="No Chrome/Chromium binary on PATH; skipping live browser E2E test",
)

# Mark the module as requiring network so CI can skip with `-m "not network"`.
pytestmark = pytestmark and pytest.mark.network

# This test needs the DB ON to track job state. Override the autouse
# _disable_db_integration fixture for this module only.
# Per pytest docs: "Marks can only be applied to tests, having no effect
# on fixtures." So we re-enable DB via a module-level fixture override.
@pytest.fixture(autouse=True)
def _enable_db_for_e2e(monkeypatch):
    """Re-enable DATABASE_INTEGRATION_ENABLED for this E2E test.

    The autouse _disable_db_integration fixture in conftest.py sets it to
    False; we override it back to True because the scraper tool writes
    job state to the operational DB.
    """
    monkeypatch.setenv("DATABASE_INTEGRATION_ENABLED", "true")
    try:
        import config
        monkeypatch.setattr(config, "DATABASE_INTEGRATION_ENABLED", True)
    except ImportError:
        pass
    yield


def test_scraper_e2e_loop(tmp_path: Path):
    """Enqueue a scraper job, poll until COMPLETED, validate the ledger.

    Target: https://example.com (stable, no JavaScript, fast load).
    Task: a trivial summarization prompt so the LLM call is minimal.
    """
    # Import here (not at module top) so skipif fires before importing app
    # (which triggers the lifespan and DB init).
    from app import app

    client = TestClient(app)

    payload = {
        "tool_name": "scraper",
        "args": {
            "target": "https://example.com",
            "task": "Summarize the main heading of this page in one sentence."
        },
        "client_metadata": {"enable_tracker": True}
    }

    # Enqueue — NO auth header (auth was removed in Step 1).
    response = client.post("/api/tools/scraper", json=payload)
    assert response.status_code == 202, (
        f"Expected 202 Accepted, got {response.status_code}: {response.text}"
    )
    job_id = response.json()["job_id"]

    # Poll for completion with exponential backoff (capped at 5s).
    # Max ~2 minutes total (30 attempts × avg 4s).
    status = "QUEUED"
    max_attempts = 30
    for i in range(max_attempts):
        status_res = client.get(f"/api/jobs/{job_id}")
        if status_res.status_code == 200:
            status = status_res.json()["status"]
            if status in ("COMPLETED", "FAILED", "CANCELLED"):
                break
        time.sleep(min(2 ** (i / 2), 5))

    assert status == "COMPLETED", (
        f"Job did not complete within {max_attempts} polls. Final status: {status}"
    )

    # Validate the execution ledger artifact.
    # Per tools/__init__.py: "ARTIFACT-AS-RECEIPT: Artifact files... are RECEIPTS
    # for auditing and debugging purposes ONLY."
    ledger_path = Path(f"artifacts/test_runs/ledger_{job_id}.json")
    assert ledger_path.exists(), (
        f"Ledger artifact not found at {ledger_path}. "
        f"Available: {list(Path('artifacts/test_runs').glob('ledger_*.json'))[:5]}"
    )

    with open(ledger_path, "r") as f:
        ledger = json.load(f)

    assert "steps" in ledger, f"Ledger missing 'steps' key. Keys: {list(ledger.keys())}"
    assert len(ledger["steps"]) > 0, "Ledger has zero steps — execution did not progress."

    # Validate the two canonical milestones that the scraper tool emits.
    assert any(step.get("action") == "MILESTONE: Initial Load" for step in ledger["steps"]), (
        "Missing 'MILESTONE: Initial Load' step — browser did not reach the target page."
    )
    assert any(step.get("action") == "MILESTONE: Success Declaration" for step in ledger["steps"]), (
        "Missing 'MILESTONE: Success Declaration' step — scraper did not declare success."
    )
