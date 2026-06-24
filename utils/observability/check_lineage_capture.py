# utils/observability/check_lineage_capture.py
"""Embedded synthetic tracer for the Activity-Driven Observability feature.

Runs real HTTP workflows against the staging server with capture_lineage=true,
receives LineageReport responses, and asserts the produced lineage against the
expected lineage shape per branch.

No mocks. No mimic. The tracer IS the test.

Invocable via: python -m utils.observability.check_lineage_capture
Returns structured result JSON to stdout. QA reads the raw output.

Per convention §4.4: "The synthetic tracer is the test runner; it triggers
every major branch of each entry point with deterministic payloads (lineage
capture ON), receives the lineage report, and asserts the produced lineage
against the expected lineage shape — not just the business response."
"""
import asyncio
import json
import os
import sys
from typing import Any

import httpx

BASE_URL = os.getenv("SYNC_HEALTH_BASE_URL", "http://localhost:8000")


async def _post_job_with_lineage(tool_name: str, args: dict, timeout: float = 120.0) -> dict:
    """POST /api/jobs with capture_lineage=true and return the sync response."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout) as client:
        resp = await client.post("/api/jobs", json={
            "tool_name": tool_name,
            "args": args,
            "capture_lineage": True,
        })
        return {
            "status_code": resp.status_code,
            "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
        }


def _extract_lineage(response_body: dict) -> list:
    """Extract the lineage list from the wrapped response.

    When capture_lineage=true, the result field is wrapped as:
    {business_response_snapshot, lineage, summary}
    """
    result = response_body.get("result", {})
    if isinstance(result, dict) and "lineage" in result:
        return result["lineage"]
    return []


def _assert_lineage_shape(
    branch_name: str,
    produced_lineage: list,
    expected_activity_names: list[str],
    expected_statuses: list[str],
) -> list[dict]:
    """Assert the produced lineage matches the expected shape.

    Returns a list of check results (each {check, pass, detail}).
    """
    checks = []

    # Check 1: activity count matches.
    produced_count = len(produced_lineage)
    expected_count = len(expected_activity_names)
    checks.append({
        "check": "activity_count",
        "pass": produced_count == expected_count,
        "detail": f"produced={produced_count}, expected={expected_count}",
    })

    # Check 2: activity names match in order.
    produced_names = [a.get("activity_name", "") for a in produced_lineage]
    if produced_names == expected_activity_names:
        checks.append({"check": "activity_names_order", "pass": True})
    else:
        checks.append({
            "check": "activity_names_order",
            "pass": False,
            "detail": f"produced={produced_names}, expected={expected_activity_names}",
        })

    # Check 3: per-activity status matches.
    for i, (activity, expected_status) in enumerate(zip(produced_lineage, expected_statuses)):
        actual_status = activity.get("status", "")
        name = activity.get("activity_name", f"#{i}")
        if actual_status == expected_status:
            checks.append({"check": f"activity[{i}]_{name}_status", "pass": True})
        else:
            checks.append({
                "check": f"activity[{i}]_{name}_status",
                "pass": False,
                "detail": f"produced={actual_status}, expected={expected_status}",
            })

    return checks


async def check_stock_financials_status_happy() -> dict:
    """Branch: stock_financials status happy path.

    Expected lineage:
      1. "Validate StockFinancialsInput" PASSED
      2. "Query Cache Status" PASSED
      3. "Build Status Markdown" PASSED
    """
    result = {"name": "stock_financials_status_happy", "status": "unknown", "checks": [], "raw": []}
    try:
        resp = await _post_job_with_lineage(
            "stock_financials",
            {"command": "status", "instructions": {"ticker": "AAPL"}},
            timeout=60.0,
        )
        result["raw"].append(f"POST /api/jobs status: {resp['status_code']}")
        if resp["status_code"] != 200:
            result["checks"].append({"check": "http_200", "pass": False, "detail": f"got {resp['status_code']}"})
            result["status"] = "unhealthy"
            return result
        result["checks"].append({"check": "http_200", "pass": True})

        body = resp["body"]
        result["raw"].append(f"response status: {body.get('status')}")
        lineage = _extract_lineage(body)
        result["raw"].append(f"lineage activities: {len(lineage)}")

        checks = _assert_lineage_shape(
            "stock_financials_status_happy",
            lineage,
            expected_activity_names=["Validate StockFinancialsInput", "Query Cache Status", "Build Status Markdown"],
            expected_statuses=["PASSED", "PASSED", "PASSED"],
        )
        result["checks"].extend(checks)

        # Verify the result is wrapped (business_response_snapshot present).
        result_field = body.get("result", {})
        if isinstance(result_field, dict) and "business_response_snapshot" in result_field:
            result["checks"].append({"check": "result_wrapped", "pass": True})
        else:
            result["checks"].append({"check": "result_wrapped", "pass": False, "detail": "business_response_snapshot missing"})

        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_stock_financials_extract_error() -> dict:
    """Branch: stock_financials extract error path (invalid ticker).

    Expected lineage:
      1. "Validate StockFinancialsInput" PASSED
      2. "Check Cache Hit" PASSED
      3. "Extract and Persist Facts" FAILED
    """
    result = {"name": "stock_financials_extract_error", "status": "unknown", "checks": [], "raw": []}
    try:
        resp = await _post_job_with_lineage(
            "stock_financials",
            {"command": "extract", "instructions": {"ticker": "INVALIDTICKER123", "quarters": 1}},
            timeout=120.0,
        )
        result["raw"].append(f"POST /api/jobs status: {resp['status_code']}")
        if resp["status_code"] != 200:
            result["checks"].append({"check": "http_200", "pass": False, "detail": f"got {resp['status_code']}"})
            result["status"] = "unhealthy"
            return result
        result["checks"].append({"check": "http_200", "pass": True})

        body = resp["body"]
        result["raw"].append(f"response status: {body.get('status')}")
        # The job should be FAILED (invalid ticker).
        if body.get("status") == "FAILED":
            result["checks"].append({"check": "job_failed", "pass": True})
        else:
            result["checks"].append({"check": "job_failed", "pass": False, "detail": f"got {body.get('status')}"})

        # The error field must be non-empty.
        error = body.get("error")
        if error and len(error) > 10:
            result["checks"].append({"check": "error_field_populated", "pass": True})
        else:
            result["checks"].append({"check": "error_field_populated", "pass": False, "detail": f"error={error!r}"})

        lineage = _extract_lineage(body)
        result["raw"].append(f"lineage activities: {len(lineage)}")

        checks = _assert_lineage_shape(
            "stock_financials_extract_error",
            lineage,
            expected_activity_names=["Validate StockFinancialsInput", "Check Cache Hit", "Extract and Persist Facts"],
            expected_statuses=["PASSED", "PASSED", "FAILED"],
        )
        result["checks"].extend(checks)

        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_draft_editor_validation_error() -> dict:
    """Branch: draft_editor input-validation failure (missing batch_id).

    Expected lineage:
      1. "Validate Batch ID" FAILED
    """
    result = {"name": "draft_editor_validation_error", "status": "unknown", "checks": [], "raw": []}
    try:
        resp = await _post_job_with_lineage(
            "draft_editor",
            {"batch_id": "", "operations": []},
            timeout=30.0,
        )
        result["raw"].append(f"POST /api/jobs status: {resp['status_code']}")
        if resp["status_code"] != 200:
            result["checks"].append({"check": "http_200", "pass": False, "detail": f"got {resp['status_code']}"})
            result["status"] = "unhealthy"
            return result
        result["checks"].append({"check": "http_200", "pass": True})

        body = resp["body"]
        result["raw"].append(f"response status: {body.get('status')}")
        if body.get("status") == "FAILED":
            result["checks"].append({"check": "job_failed", "pass": True})
        else:
            result["checks"].append({"check": "job_failed", "pass": False, "detail": f"got {body.get('status')}"})

        lineage = _extract_lineage(body)
        result["raw"].append(f"lineage activities: {len(lineage)}")

        checks = _assert_lineage_shape(
            "draft_editor_validation_error",
            lineage,
            expected_activity_names=["Validate Batch ID"],
            expected_statuses=["FAILED"],
        )
        result["checks"].extend(checks)

        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_lineage_guard_403() -> dict:
    """Branch: capture_lineage=true against non-staging server returns 403.

    This branch is only runnable when DATABASE_STAGING_ENABLED=false.
    If staging IS enabled, this branch is skipped.
    """
    result = {"name": "lineage_guard_403", "status": "unknown", "checks": [], "raw": []}
    try:
        # Check if staging is enabled by hitting the manifest (no side effects).
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
            # Try a capture_lineage request — if staging is disabled, expect 403.
            resp = await client.post("/api/jobs", json={
                "tool_name": "stock_financials",
                "args": {"command": "status", "instructions": {"ticker": "AAPL"}},
                "capture_lineage": True,
            }, timeout=10.0)

            if resp.status_code == 403:
                result["checks"].append({"check": "returns_403", "pass": True})
                result["status"] = "healthy"
            elif resp.status_code == 200:
                # Staging is enabled — skip this branch.
                result["checks"].append({"check": "skipped_staging_enabled", "pass": True, "detail": "Staging is enabled; 403 guard not applicable"})
                result["status"] = "healthy"
            else:
                result["checks"].append({"check": "returns_403", "pass": False, "detail": f"got {resp.status_code}"})
                result["status"] = "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_batch_reader_happy() -> dict:
    """Branch: batch_reader happy path (using non-existent batch to test failure point).

    Expected lineage:
      1. "Validate BatchReader Input" PASSED
      2. "Fetch Batch Article IDs" FAILED
    """
    result = {"name": "batch_reader_happy", "status": "unknown", "checks": [], "raw": []}
    try:
        resp = await _post_job_with_lineage(
            "batch_reader",
            {"batch_id": "NONEXISTENT_BATCH_ID", "query": "test query", "limit": 5},
            timeout=30.0,
        )
        result["raw"].append(f"POST /api/jobs status: {resp['status_code']}")
        if resp["status_code"] != 200:
            result["checks"].append({"check": "http_200", "pass": False, "detail": f"got {resp['status_code']}"})
            result["status"] = "unhealthy"
            return result
        result["checks"].append({"check": "http_200", "pass": True})

        body = resp["body"]
        result["raw"].append(f"response status: {body.get('status')}")
        if body.get("status") == "FAILED":
            result["checks"].append({"check": "job_failed", "pass": True})
        else:
            result["checks"].append({"check": "job_failed", "pass": False, "detail": f"got {body.get('status')}"})

        lineage = _extract_lineage(body)
        result["raw"].append(f"lineage activities: {len(lineage)}")

        checks = _assert_lineage_shape(
            "batch_reader_not_found",
            lineage,
            expected_activity_names=["Validate BatchReader Input", "Fetch Batch Article IDs"],
            expected_statuses=["PASSED", "FAILED"],
        )
        result["checks"].extend(checks)

        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_batch_reader_validation_error() -> dict:
    """Branch: batch_reader missing batch_id.

    Expected lineage:
      1. "Validate BatchReader Input" FAILED
    """
    result = {"name": "batch_reader_validation_error", "status": "unknown", "checks": [], "raw": []}
    try:
        resp = await _post_job_with_lineage(
            "batch_reader",
            {"query": "test"},
            timeout=30.0,
        )
        result["raw"].append(f"POST /api/jobs status: {resp['status_code']}")
        if resp["status_code"] != 200:
            result["checks"].append({"check": "http_200", "pass": False, "detail": f"got {resp['status_code']}"})
            result["status"] = "unhealthy"
            return result
        result["checks"].append({"check": "http_200", "pass": True})

        body = resp["body"]
        lineage = _extract_lineage(body)
        result["raw"].append(f"lineage activities: {len(lineage)}")

        checks = _assert_lineage_shape(
            "batch_reader_validation_error",
            lineage,
            expected_activity_names=["Validate BatchReader Input"],
            expected_statuses=["FAILED"],
        )
        result["checks"].extend(checks)

        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_publisher_validation_error() -> dict:
    """Branch: publisher missing batch_id.

    Expected lineage:
      1. "Validate Publisher Input" FAILED
    """
    result = {"name": "publisher_validation_error", "status": "unknown", "checks": [], "raw": []}
    try:
        resp = await _post_job_with_lineage(
            "publisher",
            {},
            timeout=30.0,
        )
        result["raw"].append(f"POST /api/jobs status: {resp['status_code']}")
        if resp["status_code"] != 200:
            result["checks"].append({"check": "http_200", "pass": False, "detail": f"got {resp['status_code']}"})
            result["status"] = "unhealthy"
            return result
        result["checks"].append({"check": "http_200", "pass": True})

        body = resp["body"]
        lineage = _extract_lineage(body)
        result["raw"].append(f"lineage activities: {len(lineage)}")

        checks = _assert_lineage_shape(
            "publisher_validation_error",
            lineage,
            expected_activity_names=["Validate Publisher Input"],
            expected_statuses=["FAILED"],
        )
        result["checks"].extend(checks)

        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_publisher_not_found() -> dict:
    """Branch: publisher with nonexistent batch_id.

    Expected lineage:
      1. "Validate Publisher Input" PASSED
      2. "Fetch Batch Info" FAILED
    """
    result = {"name": "publisher_not_found", "status": "unknown", "checks": [], "raw": []}
    try:
        resp = await _post_job_with_lineage(
            "publisher",
            {"batch_id": "NONEXISTENT_BATCH_ID"},
            timeout=30.0,
        )
        result["raw"].append(f"POST /api/jobs status: {resp['status_code']}")
        if resp["status_code"] != 200:
            result["checks"].append({"check": "http_200", "pass": False, "detail": f"got {resp['status_code']}"})
            result["status"] = "unhealthy"
            return result
        result["checks"].append({"check": "http_200", "pass": True})

        body = resp["body"]
        lineage = _extract_lineage(body)
        result["raw"].append(f"lineage activities: {len(lineage)}")

        checks = _assert_lineage_shape(
            "publisher_not_found",
            lineage,
            expected_activity_names=["Validate Publisher Input", "Fetch Batch Info"],
            expected_statuses=["PASSED", "FAILED"],
        )
        result["checks"].extend(checks)

        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_stock_notes_validation_error() -> dict:
    """Branch: stock_notes invalid command.

    Expected lineage:
      1. "Validate StockNotes Input" FAILED
    """
    result = {"name": "stock_notes_validation_error", "status": "unknown", "checks": [], "raw": []}
    try:
        resp = await _post_job_with_lineage(
            "stock_notes",
            {"command": "invalid_command"},
            timeout=30.0,
        )
        result["raw"].append(f"POST /api/jobs status: {resp['status_code']}")
        if resp["status_code"] != 200:
            result["checks"].append({"check": "http_200", "pass": False, "detail": f"got {resp['status_code']}"})
            result["status"] = "unhealthy"
            return result
        result["checks"].append({"check": "http_200", "pass": True})

        body = resp["body"]
        lineage = _extract_lineage(body)
        result["raw"].append(f"lineage activities: {len(lineage)}")

        checks = _assert_lineage_shape(
            "stock_notes_validation_error",
            lineage,
            expected_activity_names=["Validate StockNotes Input"],
            expected_statuses=["FAILED"],
        )
        result["checks"].extend(checks)

        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def check_stock_notes_discover_no_ticker() -> dict:
    """Branch: stock_notes discover without ticker.

    Expected lineage:
      1. "Validate StockNotes Input" PASSED
      2. (job fails at the missing-ticker check — this is a raise inside run(),
         not an activity, so lineage only shows the validation activity)
    """
    result = {"name": "stock_notes_discover_no_ticker", "status": "unknown", "checks": [], "raw": []}
    try:
        resp = await _post_job_with_lineage(
            "stock_notes",
            {"command": "discover", "instructions": {}},
            timeout=30.0,
        )
        result["raw"].append(f"POST /api/jobs status: {resp['status_code']}")
        if resp["status_code"] != 200:
            result["checks"].append({"check": "http_200", "pass": False, "detail": f"got {resp['status_code']}"})
            result["status"] = "unhealthy"
            return result
        result["checks"].append({"check": "http_200", "pass": True})

        body = resp["body"]
        result["raw"].append(f"response status: {body.get('status')}")
        lineage = _extract_lineage(body)
        result["raw"].append(f"lineage activities: {len(lineage)}")

        checks = _assert_lineage_shape(
            "stock_notes_discover_no_ticker",
            lineage,
            expected_activity_names=["Validate StockNotes Input"],
            expected_statuses=["PASSED"],
        )
        result["checks"].extend(checks)

        if body.get("status") == "FAILED":
            result["checks"].append({"check": "job_failed", "pass": True})
        else:
            result["checks"].append({"check": "job_failed", "pass": False, "detail": f"got {body.get('status')}"})

        result["status"] = "healthy" if all(c.get("pass") for c in result["checks"]) else "unhealthy"
    except Exception as e:
        result["status"] = "unhealthy"
        result["error"] = str(e)
    return result


async def run_all() -> dict:
    """Run all lineage capture checks and return a structured result."""
    branches = await asyncio.gather(
        check_stock_financials_status_happy(),
        check_stock_financials_extract_error(),
        check_draft_editor_validation_error(),
        check_lineage_guard_403(),
        check_batch_reader_happy(),
        check_batch_reader_validation_error(),
        check_publisher_validation_error(),
        check_publisher_not_found(),
        check_stock_notes_validation_error(),
        check_stock_notes_discover_no_ticker(),
        return_exceptions=True,
    )

    results = []
    for b in branches:
        if isinstance(b, Exception):
            results.append({"name": "exception", "status": "unhealthy", "error": str(b)})
        else:
            results.append(b)

    all_passed = all(r.get("status") == "healthy" for r in results)
    overall = "healthy" if all_passed else "unhealthy"

    return {
        "status": overall,
        "all_branches_passed": all_passed,
        "branches_evaluated": results,
    }


def main() -> int:
    result = asyncio.run(run_all())
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] == "healthy" else 1


if __name__ == "__main__":
    sys.exit(main())
