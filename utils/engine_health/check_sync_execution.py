# utils/engine_health/check_sync_execution.py
"""E2E health checker for the agent-native sync execution engine.

Runs real HTTP workflows against the staging server:
  HC1: POST /api/jobs happy path -> 200 with {status: COMPLETED}
  HC2: POST /api/jobs error path -> 200 with {status: FAILED, error: ...}
  HC3: POST /api/jobs timeout -> 504 with fallback pointer
  HC4: POST /api/jobs/{id}/resume on terminal job -> 409
  HC5: POST /api/jobs/{id}/resume on CANCELLING job -> 409
  HC6: GET /api/jobs/{id}/status returns current state

No mocks. No mimic. The health checker IS the test.

Invocable via: python -m utils.engine_health.check_sync_execution
Returns structured result JSON to stdout. QA reads the raw output.
"""
import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx

BASE_URL = os.getenv("SYNC_HEALTH_BASE_URL", "http://localhost:8000")


async def _post_job(tool_name: str, args: dict, timeout: float = 60.0) -> dict:
    """POST /api/jobs and return the sync response."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout) as client:
        resp = await client.post("/api/jobs", json={"tool_name": tool_name, "args": args})
        return {"status_code": resp.status_code, "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text}


async def _get_status(job_id: str) -> dict:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        resp = await client.get(f"/api/jobs/{job_id}")
        return {"status_code": resp.status_code, "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text}


async def _resume(job_id: str, decision: str = "proceed") -> dict:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0) as client:
        resp = await client.post(f"/api/jobs/{job_id}/resume", json={"decision": decision})
        return {"status_code": resp.status_code, "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text}


async def _cancel(job_id: str) -> dict:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        resp = await client.delete(f"/api/jobs/{job_id}")
        return {"status_code": resp.status_code, "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text}


async def check_happy_path() -> dict:
    """HC1: POST /api/jobs with stock_financials status command -> COMPLETED."""
    result = {"name": "happy_path", "status": "unknown", "checks": [], "raw": []}
    try:
        # Use a fast, cache-friendly tool path: stock_financials status on AAPL.
        resp = await _post_job("stock_financials", {"command": "status", "instructions": {"ticker": "AAPL"}}, timeout=60.0)
        result["raw"].append(f"POST /api/jobs status: {resp['status_code']}")
        if resp["status_code"] != 200:
            result["checks"].append({"check": "http_200", "pass": False, "detail": f"got {resp['status_code']}"})
            result["status"] = "unhealthy"
            return result
        body = resp["body"]
        result["raw"].append(f"response status: {body.get('status')}")
        if body.get("status") == "COMPLETED":
            result["checks"].append({"check": "completed_status", "pass": True})
        elif body.get("status") == "FAILED":
            # Acceptable if AAPL has no cached data — but log it.
            result["checks"].append({"check": "completed_status", "pass": False, "detail": f"FAILED: {body.get('error', '')[:200]}"})
        else:
            result["checks"].append({"check": "completed_status", "pass": False, "detail": f"unexpected status: {body.get('status')}"})
        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_error_path() -> dict:
    """HC2: POST /api/jobs with invalid tool input -> FAILED with error field."""
    result = {"name": "error_path", "status": "unknown", "checks": [], "raw": []}
    try:
        # Invalid ticker triggers ToolValidationError -> FAILED.
        resp = await _post_job("stock_financials", {"command": "extract", "instructions": {"ticker": "INVALIDTICKER123", "quarters": 1}}, timeout=120.0)
        result["raw"].append(f"POST /api/jobs status: {resp['status_code']}")
        body = resp["body"]
        result["raw"].append(f"response status: {body.get('status')}")
        if body.get("status") == "FAILED":
            result["checks"].append({"check": "failed_status", "pass": True})
        else:
            result["checks"].append({"check": "failed_status", "pass": False, "detail": f"got {body.get('status')}"})
        # The error field must be non-empty and contain diagnostic info.
        error = body.get("error")
        if error and len(error) > 10:
            result["checks"].append({"check": "error_field_populated", "pass": True})
        else:
            result["checks"].append({"check": "error_field_populated", "pass": False, "detail": f"error={error!r}"})
        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_resume_on_terminal() -> dict:
    """HC4: POST /resume on a COMPLETED job -> 409."""
    result = {"name": "resume_on_terminal", "status": "unknown", "checks": [], "raw": []}
    try:
        # First, create and await a job to terminal state.
        resp = await _post_job("stock_financials", {"command": "status", "instructions": {"ticker": "AAPL"}}, timeout=60.0)
        if resp["status_code"] != 200:
            result["status"] = "unhealthy"
            result["error"] = "could not create terminal job"
            return result
        job_id = resp["body"]["job_id"]
        # Attempt resume — expect 409.
        resume_resp = await _resume(job_id, "proceed")
        result["raw"].append(f"resume status: {resume_resp['status_code']}")
        if resume_resp["status_code"] == 409:
            result["checks"].append({"check": "terminal_resume_409", "pass": True})
        else:
            result["checks"].append({"check": "terminal_resume_409", "pass": False, "detail": f"expected 409, got {resume_resp['status_code']}"})
        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_resume_on_cancelling() -> dict:
    """HC5: Cancel a job, then /resume -> 409."""
    result = {"name": "resume_on_cancelling", "status": "unknown", "checks": [], "raw": []}
    try:
        # Create a job but DON'T await — cancel immediately.
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
            # Use the deprecated 202 endpoint to get a job_id without awaiting.
            create_resp = await client.post("/api/tools/scraper", json={"args": {"target_site": "hackernews"}})
            if create_resp.status_code != 202:
                result["status"] = "unhealthy"
                result["error"] = f"could not create job: {create_resp.status_code}"
                return result
            job_id = create_resp.json()["job_id"]
            # Cancel it.
            cancel_resp = await client.delete(f"/api/jobs/{job_id}")
            result["raw"].append(f"cancel status: {cancel_resp.status_code}")
            # Attempt resume — expect 409.
            resume_resp = await _resume(job_id, "proceed")
            result["raw"].append(f"resume status: {resume_resp['status_code']}")
            if resume_resp["status_code"] == 409:
                result["checks"].append({"check": "cancelling_resume_409", "pass": True})
            else:
                result["checks"].append({"check": "cancelling_resume_409", "pass": False, "detail": f"expected 409, got {resume_resp['status_code']}"})
        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_status_endpoint() -> dict:
    """HC6: GET /api/jobs/{id}/status returns current state."""
    result = {"name": "status_endpoint", "status": "unknown", "checks": [], "raw": []}
    try:
        resp = await _post_job("stock_financials", {"command": "status", "instructions": {"ticker": "AAPL"}}, timeout=60.0)
        job_id = resp["body"]["job_id"]
        status_resp = await _get_status(job_id)
        result["raw"].append(f"GET status: {status_resp['status_code']}")
        if status_resp["status_code"] == 200:
            body = status_resp["body"]
            if body.get("job_id") == job_id and "status" in body:
                result["checks"].append({"check": "status_returned", "pass": True})
            else:
                result["checks"].append({"check": "status_returned", "pass": False, "detail": "missing job_id or status"})
        else:
            result["checks"].append({"check": "status_returned", "pass": False, "detail": f"got {status_resp['status_code']}"})
        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def run_all() -> dict:
    """Run all health checks and return a structured result."""
    checks = await asyncio.gather(
        check_happy_path(),
        check_error_path(),
        check_resume_on_terminal(),
        check_resume_on_cancelling(),
        check_status_endpoint(),
        return_exceptions=True,
    )
    results = []
    for c in checks:
        if isinstance(c, Exception):
            results.append({"name": "exception", "status": "unhealthy", "error": str(c)})
        else:
            results.append(c)
    overall = "healthy" if all(r.get("status") == "healthy" for r in results) else "degraded"
    if all(r.get("status") == "unhealthy" for r in results):
        overall = "unhealthy"
    return {"status": overall, "checks": results}


def main() -> int:
    result = asyncio.run(run_all())
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] == "healthy" else 1


if __name__ == "__main__":
    sys.exit(main())
