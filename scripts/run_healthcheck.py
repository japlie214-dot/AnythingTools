#!/usr/bin/env python3
"""CLI runner for tool health checks.

Calls POST /api/health-check/{tool_name} and awaits the sync response.
Exits 0 if the job reaches the expected terminal status, non-zero otherwise.

Usage:
  python scripts/run_healthcheck.py <tool_name> [--path happy|error] [--host localhost] [--port 8000]
"""
import argparse
import json
import sys
import httpx


def run_health_check(tool_name: str, path: str, host: str, port: int, timeout: int) -> int:
    base_url = f"http://{host}:{port}"
    endpoint = f"{base_url}/api/health-check/{tool_name}?path={path}"

    print(f"Starting health check: tool={tool_name} path={path}")
    print(f"Endpoint: {endpoint}")
    print("-" * 60)

    try:
        with httpx.Client(timeout=float(timeout + 60)) as client:
            resp = client.post(endpoint)
            if resp.status_code == 403:
                print(f"ERROR: {resp.json().get('detail', 'Staging not enabled')}")
                print("Set DATABASE_STAGING_ENABLED=true and restart the server.")
                return 1
            if resp.status_code == 404:
                print(f"ERROR: Tool '{tool_name}' not found")
                return 1
            if resp.status_code == 501:
                print(f"ERROR: {resp.json().get('detail', 'Tool does not implement health_check_payload')}")
                return 1
            resp.raise_for_status()

            data = resp.json()
            job_id = data["job_id"]
            final = data.get("final_result")

        print(f"Job ID: {job_id}")
        print("-" * 60)

        if final is None:
            print("FAIL: No final_result in response")
            return 1

        print(f"Status: {final.get('status')}")
        if final.get("error"):
            print(f"Error: {final['error'][:500]}")
        if final.get("result"):
            result_str = json.dumps(final["result"], default=str)
            print(f"Result: {result_str[:500]}")

    except httpx.ConnectError:
        print(f"ERROR: Cannot connect to {base_url}. Is the server running?")
        return 1
    except Exception as e:
        print(f"ERROR: {e}")
        return 1

    expected = "COMPLETED" if path == "happy" else "FAILED"
    actual = final.get("status")
    if actual == expected:
        print(f"PASS: {tool_name} ({path}) reached expected status '{expected}'")
        return 0
    else:
        print(f"FAIL: {tool_name} ({path}) reached '{actual}', expected '{expected}'")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a tool health check against the staging database.")
    parser.add_argument("tool_name", nargs="?")
    parser.add_argument("--path", choices=["happy", "error"], default="happy")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    if args.all:
        try:
            resp = httpx.get(f"http://{args.host}:{args.port}/api/manifest", timeout=10.0)
            resp.raise_for_status()
            tools = [t["name"] for t in resp.json().get("tools", [])]
        except Exception as e:
            print(f"ERROR: Cannot fetch tool manifest: {e}")
            return 1
        all_passed = True
        for tool_name in tools:
            for path in ["happy", "error"]:
                print(f"\n{'='*60}")
                print(f"Health Check: {tool_name} ({path})")
                print(f"{'='*60}")
                if run_health_check(tool_name, path, args.host, args.port, args.timeout) != 0:
                    all_passed = False
        return 0 if all_passed else 1
    elif args.tool_name:
        return run_health_check(args.tool_name, args.path, args.host, args.port, args.timeout)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
