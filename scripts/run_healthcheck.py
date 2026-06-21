#!/usr/bin/env python3
"""CLI runner for tool health checks.

Calls POST /api/health-check/{tool_name} and streams the SSE response
to stdout. Exits 0 if the job reaches the expected terminal status,
non-zero otherwise.

Usage:
  python scripts/run_healthcheck.py <tool_name> [--path happy|error] [--host localhost] [--port 8000]

Examples:
  python scripts/run_healthcheck.py stock_financials
  python scripts/run_healthcheck.py scraper --path error
  python scripts/run_healthcheck.py --all
"""
import argparse
import json
import sys
import time
import httpx


def run_health_check(tool_name: str, path: str, host: str, port: int, timeout: int) -> int:
    base_url = f"http://{host}:{port}"
    endpoint = f"{base_url}/api/health-check/{tool_name}?path={path}"

    print(f"Starting health check: tool={tool_name} path={path}")
    print(f"Endpoint: {endpoint}")
    print("-" * 60)

    try:
        with httpx.Client(timeout=30.0) as client:
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
            stream_url = data["stream_url"]
            job_timeout = data.get("timeout_seconds", timeout)

        print(f"Job ID: {job_id}")
        print(f"Stream URL: {stream_url}")
        print(f"Timeout: {job_timeout}s")
        print("-" * 60)

        final_status = None
        start_time = time.time()

        with httpx.Client(timeout=float(job_timeout + 30)) as client:
            with client.stream("GET", stream_url) as stream:
                for line in stream.iter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        # Comment (keep-alive)
                        continue
                    if line.startswith("event:"):
                        event_type = line.split(":", 1)[1].strip()
                        print(f"[EVENT] {event_type}")
                    elif line.startswith("data:"):
                        data_str = line.split(":", 1)[1].strip()
                        try:
                            data = json.loads(data_str)
                            # Print key fields
                            if "status" in data:
                                final_status = data["status"]
                                print(f"  status: {data['status']}")
                            if "level" in data:
                                print(f"  [{data['level']}] {data.get('tag', '')}: {data.get('message', '')}")
                            if "message" in data and "level" not in data:
                                print(f"  {data['message']}")
                            if "reason" in data:
                                print(f"  reason: {data['reason']}")
                            if "error" in data:
                                print(f"  ERROR: {data['error']}")
                                if data.get("traceback"):
                                    print(f"  Traceback:\n{data['traceback']}")
                        except json.JSONDecodeError:
                            print(f"  {data_str}")

                        if event_type == "stream.end":
                            print("-" * 60)
                            print(f"Stream ended. Final status: {final_status}")
                            break

                    if time.time() - start_time > job_timeout:
                        print(f"TIMEOUT after {job_timeout}s")
                        return 1

    except httpx.ConnectError:
        print(f"ERROR: Cannot connect to {base_url}. Is the server running?")
        return 1
    except Exception as e:
        print(f"ERROR: {e}")
        return 1

    expected = "COMPLETED" if path == "happy" else "FAILED"
    if final_status == expected:
        print(f"PASS: {tool_name} ({path}) reached expected status '{expected}'")
        return 0
    else:
        print(f"FAIL: {tool_name} ({path}) reached '{final_status}', expected '{expected}'")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a tool health check against the staging database.",
        epilog="Examples:\n  python scripts/run_healthcheck.py stock_financials\n  python scripts/run_healthcheck.py scraper --path error\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("tool_name", nargs="?", help="Name of the tool to health-check")
    parser.add_argument("--path", choices=["happy", "error"], default="happy", help="Which path to test (default: happy)")
    parser.add_argument("--host", default="localhost", help="Server host (default: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="Server port (default: 8000)")
    parser.add_argument("--timeout", type=int, default=300, help="Timeout in seconds (default: 300)")
    parser.add_argument("--all", action="store_true", help="Run health checks for all registered tools")

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
                result = run_health_check(tool_name, path, args.host, args.port, args.timeout)
                if result != 0:
                    all_passed = False
        return 0 if all_passed else 1
    elif args.tool_name:
        return run_health_check(args.tool_name, args.path, args.host, args.port, args.timeout)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
