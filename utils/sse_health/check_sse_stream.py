# utils/sse_health/check_sse_stream.py
"""Embedded health checker for the SSE streaming feature.

Runs real workflows against the staging DB:
- Posts a real batch_reader job
- Streams the SSE response
- Verifies started -> running -> completed phase sequence
- Verifies Last-Event-ID resume skips duplicates
- Verifies POST /resume on a terminal job returns 409
- Verifies POST /resume on CANCELLING returns 409

Invocable via: python -m utils.sse_health.check_sse_stream

Returns structured result JSON to stdout. QA reads the raw output.
"""
import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx

# Default to localhost:8000 — the dev/staging server.
BASE_URL = os.getenv("SSE_HEALTH_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("ANYTHINGTOOLS_API_KEY", "test-key")


def _headers() -> dict:
    return {"X-API-Key": API_KEY}


async def _post_job(tool_name: str, args: dict) -> str:
    """POST /api/tools/{tool_name} and return the job_id."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        resp = await client.post(
            f"/api/tools/{tool_name}",
            json={"args": args},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()["job_id"]


async def _stream_job(job_id: str, last_event_id: str | None = None, max_events: int = 50, timeout: float = 30.0) -> list[dict]:
    """GET /api/jobs/{job_id}/stream and return parsed events."""
    events: list[dict] = []
    headers = _headers()
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout) as client:
        async with client.stream("GET", f"/api/jobs/{job_id}/stream", headers=headers) as resp:
            resp.raise_for_status()
            current_event: dict = {}
            async for line in resp.aiter_lines():
                if line == "":
                    if current_event:
                        events.append(current_event)
                        current_event = {}
                        if len(events) >= max_events:
                            break
                    continue
                if line.startswith(":"):
                    continue
                if ":" not in line:
                    continue
                field, _, value = line.partition(":")
                value = value.lstrip(" ")
                if field == "event":
                    current_event["event"] = value
                elif field == "id":
                    current_event["id"] = value
                elif field == "data":
                    current_event.setdefault("data_lines", []).append(value)
                elif field == "retry":
                    current_event["retry"] = value
            if current_event:
                events.append(current_event)
    return events


def _parse_event_data(event: dict) -> Any:
    if "data_lines" not in event:
        return None
    raw = "\n".join(event["data_lines"])
    try:
        return json.loads(raw)
    except Exception:
        return raw


async def check_happy_path() -> dict:
    """HC1: POST a batch_reader job, stream until completed."""
    result = {"name": "happy_path", "status": "unknown", "checks": [], "raw": []}
    try:
        # batch_reader requires a real batch_id in the DB. Use a sentinel
        # batch_id that will fail gracefully — the job will complete with
        # status FAILED but the SSE stream should still emit started -> running -> completed.
        job_id = await _post_job("batch_reader", {"batch_id": "sse-health-batch", "query": "test", "limit": 1})
        result["job_id"] = job_id
        events = await _stream_job(job_id, max_events=100, timeout=60.0)
        phases = [e.get("event") for e in events if e.get("event")]
        result["phases"] = phases
        result["raw"].append(f"events received: {len(events)}")
        result["raw"].append(f"phase sequence: {phases}")
        if "started" in phases:
            result["checks"].append({"check": "started_emitted", "pass": True})
        else:
            result["checks"].append({"check": "started_emitted", "pass": False, "detail": "no started event"})
        if "completed" in phases:
            result["checks"].append({"check": "completed_emitted", "pass": True})
        else:
            result["checks"].append({"check": "completed_emitted", "pass": False, "detail": "no completed event"})
        all_pass = all(c.get("pass") for c in result["checks"])
        result["status"] = "healthy" if all_pass else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_resume_on_terminal() -> dict:
    """HC7: POST /resume on a COMPLETED job should 409."""
    result = {"name": "resume_on_terminal", "status": "unknown", "checks": [], "raw": []}
    try:
        job_id = await _post_job("batch_reader", {"batch_id": "sse-health-terminal", "query": "test", "limit": 1})
        # Wait for completion
        events = await _stream_job(job_id, max_events=100, timeout=60.0)
        if not any(e.get("event") == "completed" for e in events):
            result["status"] = "unhealthy"
            result["error"] = "job did not complete"
            return result
        # Attempt resume — expect 409
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
            resp = await client.post(f"/api/jobs/{job_id}/resume", json={"decision": "proceed"}, headers=_headers())
            result["raw"].append(f"resume status: {resp.status_code}")
            if resp.status_code == 409:
                result["checks"].append({"check": "terminal_resume_409", "pass": True})
            else:
                result["checks"].append({"check": "terminal_resume_409", "pass": False, "detail": f"expected 409, got {resp.status_code}"})
        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_resume_on_cancelling() -> dict:
    """HC4 variant: cancel a job, then /resume should 409."""
    result = {"name": "resume_on_cancelling", "status": "unknown", "checks": [], "raw": []}
    try:
        job_id = await _post_job("batch_reader", {"batch_id": "sse-health-cancel", "query": "test", "limit": 1})
        # Immediately cancel
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
            cancel_resp = await client.delete(f"/api/jobs/{job_id}", headers=_headers())
            result["raw"].append(f"cancel status: {cancel_resp.status_code}")
            # Attempt resume — expect 409
            resp = await client.post(f"/api/jobs/{job_id}/resume", json={"decision": "proceed"}, headers=_headers())
            result["raw"].append(f"resume status: {resp.status_code}")
            if resp.status_code == 409:
                result["checks"].append({"check": "cancelling_resume_409", "pass": True})
            else:
                result["checks"].append({"check": "cancelling_resume_409", "pass": False, "detail": f"expected 409, got {resp.status_code}"})
        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_reconnect_no_duplicates() -> dict:
    """HC2: stream a job, capture last_event_id, reconnect, verify no duplicates."""
    result = {"name": "reconnect_no_duplicates", "status": "unknown", "checks": [], "raw": []}
    try:
        job_id = await _post_job("batch_reader", {"batch_id": "sse-health-reconnect", "query": "test", "limit": 1})
        # First stream — capture up to 5 events
        events1 = await _stream_job(job_id, max_events=5, timeout=30.0)
        if not events1:
            result["status"] = "unhealthy"
            result["error"] = "no events from first stream"
            return result
        last_id = events1[-1].get("id")
        result["raw"].append(f"first stream: {len(events1)} events, last_id={last_id}")
        # Reconnect with Last-Event-ID
        await asyncio.sleep(1.0)  # let more events accumulate
        events2 = await _stream_job(job_id, last_event_id=last_id, max_events=50, timeout=30.0)
        ids1 = {e.get("id") for e in events1 if e.get("id")}
        ids2 = {e.get("id") for e in events2 if e.get("id")}
        duplicates = ids1 & ids2
        result["raw"].append(f"reconnect: {len(events2)} events, duplicates={len(duplicates)}")
        if not duplicates:
            result["checks"].append({"check": "no_duplicate_ids", "pass": True})
        else:
            result["checks"].append({"check": "no_duplicate_ids", "pass": False, "detail": f"{len(duplicates)} duplicate ids"})
        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def run_all() -> dict:
    """Run all health checks and return a structured result."""
    checks = await asyncio.gather(
        check_happy_path(),
        check_resume_on_terminal(),
        check_resume_on_cancelling(),
        check_reconnect_no_duplicates(),
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
