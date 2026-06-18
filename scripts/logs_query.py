#!/usr/bin/env python3
# scripts/logs_query.py
"""LLM-friendly CLI for querying logs.db.

Design constraints:
- Zero imports from the AnythingTools project. Importing project modules
  (database.connection, utils.logger, config) triggers Snowflake connector
  init, FastAPI middleware, sqlite_vec loading, and dual-logger setup —
  none of which belong in a read-only inspection tool.
- Opens logs.db in read-only URI mode (file:...?mode=ro) so that no
  write side-effect can ever occur, even under bugs or accidents.
- Safe to run while the server is running: WAL mode (already enabled on
  logs.db by database/connection.py:LogsDatabaseManager) allows concurrent
  readers without blocking the writer.
- Output is markdown by default (LLM-friendly); --json switches to
  a single JSON array for programmatic consumers.
- Does NOT create artifacts or send anything to AnythingLLM.

PATH RESOLUTION (4-tier chain):
  1. --db <path>            (explicit CLI argument; highest priority)
  2. $LOGS_DB_PATH          (environment variable)
  3. $OPERATIONAL_DB_PATH/../logs.db
                           (parent of the operational DB path, mirroring
                           the convention in database/connection.py)
  4. ./data/logs.db         (default; lowest priority)

Usage examples:
    python scripts/logs_query.py recent --limit 20
    python scripts/logs_query.py errors --since 1h
    python scripts/logs_query.py by-tag Backup:Cloud --limit 50
    python scripts/logs_query.py by-job 01J5Q... --limit 100
    python scripts/logs_query.py search "session expired" --limit 10
    python scripts/logs_query.py show 01J5Q...
    python scripts/logs_query.py stats
    python scripts/logs_query.py tags --limit 30
    python scripts/logs_query.py tail --interval 2

References:
- SQLite read-only URI (mode=ro):
  https://docs.python.org/3/library/sqlite3.html#sqlite3.connect
  Quote: "uri (bool) – If set to True, database is interpreted as a URI
  with a file path and an optional query string. The scheme part must be
  'file:'"
- SQLite WAL concurrency (readers don't block writers):
  https://www.sqlite.org/wal.html
  Quote: "WAL provides more concurrency as readers do not block writers
  and a writer does not block readers. Reading and writing can proceed
  concurrently."
- SQLite query_only PRAGMA (weaker than mode=ro; not used here):
  https://www.sqlite.org/pragma.html#pragma_query_only
  Quote: "the database is not truly read-only" when using query_only —
  checkpoints and commits can still occur. With mode=ro, the file is
  opened read-only at the OS level and no write of any kind is possible.
- argparse subcommands pattern:
  https://docs.python.org/3/library/argparse.html#subcommands
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_LOGS_DB_PATH = Path("data/logs.db")
DEFAULT_LIMIT = 50
MAX_LIMIT = 5000
# Per SQLite WAL docs (https://www.sqlite.org/wal.html section 9), rare
# SQLITE_BUSY cases can occur during checkpoint, last-connection cleanup,
# or post-crash recovery. busy_timeout=5000 tells SQLite to wait up to
# 5 seconds for the lock to clear before raising SQLITE_BUSY.
BUSY_TIMEOUT_MS = 5000


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def _resolve_db_path(explicit: Optional[str]) -> Path:
    """Resolve the logs.db path via a 4-tier chain.

    Priority:
    1. explicit --db argument
    2. LOGS_DB_PATH environment variable
    3. OPERATIONAL_DB_PATH env var's parent directory + 'logs.db'
       (mirrors the convention used by database/connection.py:_resolve_db_path)
    4. ./data/logs.db (default)
    """
    if explicit:
        p = Path(explicit)
    elif env := os.environ.get("LOGS_DB_PATH"):
        p = Path(env)
    elif op_path := os.environ.get("OPERATIONAL_DB_PATH"):
        # If the operator set OPERATIONAL_DB_PATH=data/sumanal.db, then
        # logs.db should be at data/logs.db (same parent directory).
        # This mirrors the convention in database/connection.py where
        # DB_PATH comes from OPERATIONAL_DB_PATH and LOGS_DB_PATH is
        # currently hardcoded to Path("data") / "logs.db". This CLI
        # respects the env var so that shifting the operational folder
        # also shifts the logs folder.
        p = Path(op_path).parent / "logs.db"
    else:
        p = DEFAULT_LOGS_DB_PATH
    return p.resolve()


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open logs.db in read-only URI mode.

    Per the Python sqlite3 docs:
    https://docs.python.org/3/library/sqlite3.html#sqlite3.connect
    "uri (bool) – If set to True, database is interpreted as a URI with a
    file path and an optional query string. The scheme part must be 'file:'"

    The 'mode=ro' parameter opens the file read-only at the VFS level,
    which is stronger than the 'PRAGMA query_only=ON' approach. Per the
    SQLite docs (https://www.sqlite.org/pragma.html#pragma_query_only):
    "the database is not truly read-only" when using query_only —
    checkpoints and commits can still occur. With mode=ro, the file is
    opened read-only at the OS level and no write of any kind is possible.
    """
    if not db_path.exists():
        raise FileNotFoundError(
            f"logs.db not found at: {db_path}\n"
            f"Specify the path with --db <path> or set LOGS_DB_PATH env var."
        )
    # Use URI form: file:<absolute_path>?mode=ro
    # The path must be absolute for the file: URI to resolve correctly
    # across working directories.
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # busy_timeout helps with rare SQLITE_BUSY during checkpoint.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

def _parse_since(since: Optional[str]) -> Optional[str]:
    """Parse a human-friendly 'since' expression into an ISO 8601 timestamp.

    Supports:
    - Plain ISO 8601: 2026-06-17T08:00:00 (with or without timezone)
    - Relative: 30m, 1h, 2d, 1w (minutes, hours, days, weeks)
    """
    if not since:
        return None
    since = since.strip()
    # Try relative form first (e.g. "30m", "1h", "2d", "1w").
    if since[-1] in ("m", "h", "d", "w") and since[:-1].isdigit():
        n = int(since[:-1])
        unit = since[-1]
        delta = {
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
        }[unit]
        return (datetime.now(timezone.utc) - delta).isoformat()
    # Try ISO 8601.
    try:
        dt = datetime.fromisoformat(since)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(f"Cannot parse --since value: {since!r}")


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def _build_where(
    *,
    level: Optional[str] = None,
    tag_prefix: Optional[str] = None,
    job_id: Optional[str] = None,
    since: Optional[str] = None,
    search: Optional[str] = None,
) -> tuple[str, list[Any]]:
    """Build a WHERE clause and parameter list from filter args.

    All user-supplied values are passed as SQL parameters (never string-
    interpolated) to prevent SQL injection. This is the standard pattern
    per https://docs.python.org/3/library/sqlite3.html#sqlite3.Cursor.execute
    "Use ? as a placeholder for values."
    """
    clauses = []
    params: list[Any] = []
    if level:
        # Match the level or any level "above" it.
        # Level ordering: DEBUG < INFO < WARNING < ERROR < CRITICAL.
        level_order = {"DEBUG": 1, "INFO": 2, "WARNING": 3, "ERROR": 4, "CRITICAL": 5}
        if level.upper() in level_order:
            threshold = level_order[level.upper()]
            levels_above = [k for k, v in level_order.items() if v >= threshold]
            placeholders = ",".join("?" for _ in levels_above)
            clauses.append(f"level IN ({placeholders})")
            params.extend(levels_above)
        else:
            clauses.append("level = ?")
            params.append(level)
    if tag_prefix:
        # Match tag prefix (e.g. "Backup:Cloud" matches "Backup:Cloud:SessionRecovered").
        clauses.append("tag LIKE ?")
        params.append(tag_prefix.rstrip(":") + ":%")
    if job_id:
        clauses.append("job_id = ?")
        params.append(job_id)
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if search:
        # Case-insensitive substring search on message.
        clauses.append("LOWER(message) LIKE LOWER(?)")
        params.append(f"%{search}%")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def _render_markdown_table(rows: list[sqlite3.Row]) -> str:
    """Render log rows as a markdown table."""
    if not rows:
        return "_No log entries found._"
    header = "| Timestamp | Level | Tag | Job ID | Message |\n"
    sep =    "|-----------|-------|-----|--------|---------|\n"
    lines = []
    for r in rows:
        ts = r["timestamp"]
        # Truncate timestamp to 19 chars (YYYY-MM-DDTHH:MM:SS) for readability.
        ts_short = ts[:19] if ts else ""
        level = r["level"] or ""
        tag = r["tag"] or ""
        job = (r["job_id"] or "")[:8]
        # Escape pipe characters and newlines in the message to keep the
        # markdown table well-formed.
        msg = (r["message"] or "").replace("|", "\\|").replace("\n", " ")
        # Truncate long messages for the table view; use 'show' for full text.
        if len(msg) > 120:
            msg = msg[:117] + "..."
        lines.append(f"| {ts_short} | {level} | {tag} | {job} | {msg} |")
    return header + sep + "\n".join(lines)


def _render_markdown_detail(row: sqlite3.Row) -> str:
    """Render a single log row as a detailed markdown section."""
    lines = [
        f"## Log Entry `{row['id']}`",
        "",
        f"- **Timestamp**: `{row['timestamp']}`",
        f"- **Level**: `{row['level']}`",
        f"- **Tag**: `{row['tag']}`",
    ]
    if row["job_id"]:
        lines.append(f"- **Job ID**: `{row['job_id']}`")
    if row["status_state"]:
        lines.append(f"- **Status State**: `{row['status_state']}`")
    if row["event_id"]:
        lines.append(f"- **Event ID**: `{row['event_id']}`")
    lines.append("")
    lines.append("### Message")
    lines.append("")
    lines.append("```")
    lines.append(row["message"] or "")
    lines.append("```")
    if row["payload_json"]:
        lines.append("")
        lines.append("### Payload")
        lines.append("")
        try:
            payload = json.loads(row["payload_json"])
            lines.append("```json")
            lines.append(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            lines.append("```")
        except Exception as e:
            # Malformed payload_json — show raw text with a warning.
            lines.append("_Failed to parse payload_json as JSON:_")
            lines.append("")
            lines.append("```")
            lines.append(row["payload_json"])
            lines.append("```")
            lines.append(f"_Parse error: {e}_")
    if row["error_json"]:
        lines.append("")
        lines.append("### Error")
        lines.append("")
        try:
            err = json.loads(row["error_json"])
            lines.append("```json")
            lines.append(json.dumps(err, indent=2, ensure_ascii=False, default=str))
            lines.append("```")
        except Exception as e:
            lines.append("```")
            lines.append(row["error_json"])
            lines.append("```")
            lines.append(f"_Parse error: {e}_")
    return "\n".join(lines)


def _render_json(rows: list[sqlite3.Row]) -> str:
    """Render log rows as a JSON array.

    For convenience, payload_json and error_json fields are parsed into
    structured objects (if valid JSON) so consumers don't have to re-parse.
    """
    out = []
    for r in rows:
        d = dict(r)
        for field in ("payload_json", "error_json"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except Exception:
                    pass  # Leave as raw string if not valid JSON.
        out.append(d)
    return json.dumps(out, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------

def cmd_recent(args) -> int:
    db_path = _resolve_db_path(args.db)
    try:
        conn = _open_readonly(db_path)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        since = _parse_since(args.since)
        where, params = _build_where(level=args.level, since=since)
        sql = f"SELECT * FROM logs{where} ORDER BY timestamp DESC LIMIT ?"
        params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
        if args.json:
            print(_render_json(rows))
        else:
            print(_render_markdown_table(rows))
        return 0
    finally:
        conn.close()


def cmd_errors(args) -> int:
    """Shortcut for 'recent --level ERROR'."""
    args.level = "ERROR"
    return cmd_recent(args)


def cmd_by_tag(args) -> int:
    db_path = _resolve_db_path(args.db)
    try:
        conn = _open_readonly(db_path)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        since = _parse_since(args.since)
        where, params = _build_where(tag_prefix=args.tag_prefix, since=since)
        sql = f"SELECT * FROM logs{where} ORDER BY timestamp DESC LIMIT ?"
        params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
        if args.json:
            print(_render_json(rows))
        else:
            print(_render_markdown_table(rows))
        return 0
    finally:
        conn.close()


def cmd_by_job(args) -> int:
    db_path = _resolve_db_path(args.db)
    try:
        conn = _open_readonly(db_path)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        sql = "SELECT * FROM logs WHERE job_id = ? ORDER BY timestamp ASC LIMIT ?"
        rows = conn.execute(sql, (args.job_id, args.limit)).fetchall()
        if args.json:
            print(_render_json(rows))
        else:
            print(_render_markdown_table(rows))
        return 0
    finally:
        conn.close()


def cmd_search(args) -> int:
    db_path = _resolve_db_path(args.db)
    try:
        conn = _open_readonly(db_path)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        where, params = _build_where(search=args.query, level=args.level)
        sql = f"SELECT * FROM logs{where} ORDER BY timestamp DESC LIMIT ?"
        params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
        if args.json:
            print(_render_json(rows))
        else:
            print(_render_markdown_table(rows))
        return 0
    finally:
        conn.close()


def cmd_show(args) -> int:
    db_path = _resolve_db_path(args.db)
    try:
        conn = _open_readonly(db_path)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        row = conn.execute("SELECT * FROM logs WHERE id = ?", (args.log_id,)).fetchone()
        if not row:
            print(f"Log entry not found: {args.log_id}", file=sys.stderr)
            return 1
        if args.json:
            d = dict(row)
            for field in ("payload_json", "error_json"):
                if d.get(field):
                    try:
                        d[field] = json.loads(d[field])
                    except Exception:
                        pass
            print(json.dumps(d, indent=2, ensure_ascii=False, default=str))
        else:
            print(_render_markdown_detail(row))
        return 0
    finally:
        conn.close()


def cmd_stats(args) -> int:
    db_path = _resolve_db_path(args.db)
    try:
        conn = _open_readonly(db_path)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        since = _parse_since(args.since)
        where_clause = ""
        params: list[Any] = []
        if since:
            where_clause = " WHERE timestamp >= ?"
            params.append(since)
        # Counts by level
        level_counts = dict(
            conn.execute(
                f"SELECT level, COUNT(*) FROM logs{where_clause} GROUP BY level ORDER BY COUNT(*) DESC",
                params,
            ).fetchall()
        )
        # Counts by tag (top N)
        tag_counts = conn.execute(
            f"SELECT tag, COUNT(*) FROM logs{where_clause} GROUP BY tag ORDER BY COUNT(*) DESC LIMIT ?",
            params + [args.limit],
        ).fetchall()
        # Total count + time range
        total = conn.execute(
            f"SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM logs{where_clause}",
            params,
        ).fetchone()
        # Recent error rate (last 1h)
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        recent_errors = conn.execute(
            "SELECT COUNT(*) FROM logs WHERE timestamp >= ? AND level IN ('ERROR', 'CRITICAL')",
            (one_hour_ago,),
        ).fetchone()[0]

        if args.json:
            out = {
                "total": total[0],
                "earliest": total[1],
                "latest": total[2],
                "by_level": level_counts,
                "by_tag_top": [{"tag": r[0], "count": r[1]} for r in tag_counts],
                "errors_last_1h": recent_errors,
            }
            print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        else:
            print("# Logs Statistics")
            print("")
            print(f"- **Total entries**: {total[0]}")
            print(f"- **Earliest**: `{total[1]}`")
            print(f"- **Latest**: `{total[2]}`")
            print(f"- **Errors (last 1h)**: {recent_errors}")
            print("")
            print("## By Level")
            print("")
            print("| Level | Count |")
            print("|-------|-------|")
            for level, count in level_counts.items():
                print(f"| {level} | {count} |")
            print("")
            print(f"## Top {len(tag_counts)} Tags")
            print("")
            print("| Tag | Count |")
            print("|-----|-------|")
            for r in tag_counts:
                print(f"| {r[0]} | {r[1]} |")
        return 0
    finally:
        conn.close()


def cmd_tags(args) -> int:
    db_path = _resolve_db_path(args.db)
    try:
        conn = _open_readonly(db_path)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        since = _parse_since(args.since)
        where_clause = ""
        params: list[Any] = []
        if since:
            where_clause = " WHERE timestamp >= ?"
            params.append(since)
        rows = conn.execute(
            f"SELECT tag, COUNT(*) as cnt, MAX(timestamp) as latest "
            f"FROM logs{where_clause} GROUP BY tag ORDER BY cnt DESC LIMIT ?",
            params + [args.limit],
        ).fetchall()
        if args.json:
            print(json.dumps(
                [{"tag": r[0], "count": r[1], "latest": r[2]} for r in rows],
                indent=2,
            ))
        else:
            print("# Tags")
            print("")
            print("| Tag | Count | Latest |")
            print("|-----|-------|--------|")
            for r in rows:
                print(f"| {r[0]} | {r[1]} | {r[2][:19] if r[2] else ''} |")
        return 0
    finally:
        conn.close()


def cmd_tail(args) -> int:
    """Follow mode: poll logs.db every N seconds and print new entries.

    The connection is re-opened on each poll to force a fresh WAL end-mark,
    ensuring the reader sees new commits. Holding one connection open would
    also work in WAL mode (per https://www.sqlite.org/wal.html), but
    reconnecting is simpler and avoids stale-schema issues if a migration
    has run.
    """
    db_path = _resolve_db_path(args.db)
    try:
        conn = _open_readonly(db_path)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        # Start from the most recent log's timestamp.
        row = conn.execute("SELECT MAX(timestamp) FROM logs").fetchone()
        last_ts = row[0] if row and row[0] else datetime.now(timezone.utc).isoformat()
        print(f"# Tailing logs.db (interval={args.interval}s, Ctrl+C to stop)")
        print("")
        try:
            while True:
                time.sleep(args.interval)
                # Reconnect to refresh WAL snapshot.
                conn.close()
                conn = _open_readonly(db_path)
                rows = conn.execute(
                    "SELECT * FROM logs WHERE timestamp > ? ORDER BY timestamp ASC",
                    (last_ts,),
                ).fetchall()
                for r in rows:
                    last_ts = r["timestamp"]
                    ts = r["timestamp"][:19] if r["timestamp"] else ""
                    level = r["level"] or ""
                    tag = r["tag"] or ""
                    msg = (r["message"] or "").replace("\n", " ")
                    print(f"| {ts} | {level} | {tag} | {msg} |")
                sys.stdout.flush()
        except KeyboardInterrupt:
            print("\n_Stopped._", file=sys.stderr)
            return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser using the canonical argparse subcommand pattern.

    Reference: https://docs.python.org/3/library/argparse.html#subcommands
    "One particularly effective way of handling subcommands is to combine
    the use of the add_subparsers() method with calls to set_defaults()
    so that each subparser knows which Python function it should execute."
    """
    parser = argparse.ArgumentParser(
        prog="logs_query",
        description=(
            "LLM-friendly CLI for querying logs.db. Outputs markdown by "
            "default; use --json for structured output. Safe to run while "
            "the server is running (opens logs.db in read-only URI mode)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  recent          python scripts/logs_query.py recent --limit 20
  errors          python scripts/logs_query.py errors --since 1h
  by-tag          python scripts/logs_query.py by-tag Backup:Cloud --limit 50
  by-job          python scripts/logs_query.py by-job 01J5Q...
  search          python scripts/logs_query.py search "session expired"
  show            python scripts/logs_query.py show 01J5Q...
  stats           python scripts/logs_query.py stats
  tags            python scripts/logs_query.py tags --limit 30
  tail            python scripts/logs_query.py tail --interval 2

Output formats:
  default         Markdown table (list commands) or markdown section (show)
  --json          JSON array (list commands) or JSON object (show)

Read-only:
  The script opens logs.db in read-only URI mode (mode=ro). It is safe
  to run while the server is running. WAL mode on logs.db ensures
  readers do not block writers.

Environment variables:
  LOGS_DB_PATH          Override default logs.db path (highest-priority env var)
  OPERATIONAL_DB_PATH   If set, logs.db is read from its parent directory
""",
    )
    parser.add_argument(
        "--db",
        help="Path to logs.db (overrides env vars; default: ./data/logs.db)",
        default=None,
    )
    parser.add_argument(
        "--json",
        help="Emit JSON instead of markdown",
        action="store_true",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommand")

    def _add_list_args(p: argparse.ArgumentParser) -> None:
        """Add common list/filter arguments shared by list commands."""
        p.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_LIMIT,
            help=f"Max rows to return (default: {DEFAULT_LIMIT}, max: {MAX_LIMIT})",
        )
        p.add_argument(
            "--level",
            default=None,
            help=(
                "Filter by level (DEBUG/INFO/WARNING/ERROR/CRITICAL). "
                "For 'errors' subcommand, defaults to ERROR and above."
            ),
        )
        p.add_argument(
            "--since",
            default=None,
            help=(
                "Only entries since this time. ISO 8601 "
                "(2026-06-17T08:00:00) or relative (30m, 1h, 2d, 1w)."
            ),
        )

    # recent
    p_recent = subparsers.add_parser("recent", help="Most recent log entries")
    _add_list_args(p_recent)
    p_recent.set_defaults(func=cmd_recent)

    # errors
    p_errors = subparsers.add_parser("errors", help="All ERROR and CRITICAL entries")
    _add_list_args(p_errors)
    p_errors.set_defaults(func=cmd_errors)

    # by-tag
    p_tag = subparsers.add_parser("by-tag", help="Filter by tag prefix (e.g. Backup:Cloud)")
    p_tag.add_argument(
        "tag_prefix",
        help="Tag prefix to match (e.g. 'Backup:Cloud' matches 'Backup:Cloud:SessionRecovered')",
    )
    _add_list_args(p_tag)
    p_tag.set_defaults(func=cmd_by_tag)

    # by-job
    p_job = subparsers.add_parser("by-job", help="All entries for a specific job_id")
    p_job.add_argument("job_id", help="The job_id to filter on")
    _add_list_args(p_job)
    p_job.set_defaults(func=cmd_by_job)

    # search
    p_search = subparsers.add_parser("search", help="Full-text search on message field")
    p_search.add_argument("query", help="Search query (case-insensitive substring match)")
    _add_list_args(p_search)
    p_search.set_defaults(func=cmd_search)

    # show
    p_show = subparsers.add_parser("show", help="Show full detail of a single log entry")
    p_show.add_argument("log_id", help="The log entry id (ULID)")
    p_show.set_defaults(func=cmd_show)

    # stats
    p_stats = subparsers.add_parser("stats", help="Aggregate statistics")
    _add_list_args(p_stats)
    p_stats.set_defaults(func=cmd_stats)

    # tags
    p_tags = subparsers.add_parser("tags", help="List unique tags with counts")
    _add_list_args(p_tags)
    p_tags.set_defaults(func=cmd_tags)

    # tail
    p_tail = subparsers.add_parser("tail", help="Follow mode: poll for new entries")
    p_tail.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Polling interval in seconds (default: 2.0)",
    )
    p_tail.set_defaults(func=cmd_tail)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Clamp limit to MAX_LIMIT to prevent accidental huge queries that
    # could exhaust memory in the LLM agent consuming the output.
    if hasattr(args, "limit") and args.limit:
        args.limit = min(args.limit, MAX_LIMIT)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
