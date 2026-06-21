# api/sse/projector.py
"""SSE generator for streaming job execution events to AI agents.

Architecture:
- Holds ONE raw sqlite3.Connection to logs.db for the stream lifetime. Bypasses
  LogsDatabaseManager.get_read_connection() because that method force-closes
  and recreates the connection on every write generation bump
  (database/connection.py:121-128), causing catastrophic churn under 200ms
  polling. Per Pushback 1.
- Derives SSE phase EXCLUSIVELY from logs.status_state (single queue,
  monotonic by event_id). Does NOT poll jobs.status — that column races with
  the logs queue (per Pushback 2).
- Wakes on LogNotifyBus event (immediate) with 1s polling fallback.
- Emits a `: server shutting down` comment and returns cleanly when
  SseShutdownRegistry signals.

Refs:
- WHATWG SSE: https://html.spec.whatwg.org/multipage/server-sent-events.html
- SQLite WAL: https://www.sqlite.org/wal.html
- asyncio.Event: https://docs.python.org/3/library/asyncio-sync.html
"""
import asyncio
import json
import sqlite3
from pathlib import Path
from typing import AsyncGenerator, Optional

from api.sse.envelope import format_sse_event
from api.sse.phases import derive_phase, is_terminal
from api.sse import log_notify, shutdown
from database.connection import LOGS_DB_PATH


def _open_stream_connection() -> sqlite3.Connection:
    """Open a dedicated read-only connection for the SSE stream.

    Uses check_same_thread=False because the connection is created on the
    FastAPI event loop thread but read from the same thread throughout the
    stream's lifetime (async generator does not yield across threads).
    query_only=ON prevents accidental writes. Ref:
    https://docs.python.org/3/library/sqlite3.html#sqlite3.connect
    """
    LOGS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(LOGS_DB_PATH),
        timeout=30.0,
        check_same_thread=False,
        uri=True,
    )
    conn.row_factory = sqlite3.Row
    # WAL mode + read-only: readers don't block writers. Ref:
    # https://www.sqlite.org/wal.html
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _row_to_event_data(row: sqlite3.Row) -> dict:
    """Convert a logs.db row to the SSE `data` payload."""
    return {
        "timestamp": row["timestamp"],
        "level": row["level"],
        "tag": row["tag"],
        "status_state": row["status_state"],
        "message": row["message"],
        "payload": json.loads(row["payload_json"]) if row["payload_json"] else None,
        "event_id": row["event_id"],
    }


async def stream_job(
    job_id: str,
    last_event_id: Optional[str],
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted strings for a job's execution.

    Phase flow:
    1. Emit `started` event immediately (client knows the stream is live).
    2. Replay historical logs (event_id > last_event_id) as `running` events.
       Capped at SSE_MAX_HISTORY_ROWS to prevent OOM (per Pushback 6).
    3. Tail live logs via LogNotifyBus + 1s polling fallback.
    4. Emit `paused` when logs.status_state == 'PAUSED_FOR_HITL' is observed.
       Stream terminates after the paused event (client must reconnect).
    5. Emit `completed` when a terminal status_state is observed. Stream
       terminates.
    6. Emit `: server shutting down` comment when SseShutdownRegistry fires.
       Stream terminates.
    """
    import config

    conn = _open_stream_connection()
    notify_event = log_notify.register(job_id)
    last_seen_id = last_event_id or ""
    last_phase = "started"
    has_emitted_started = False

    try:
        # 1. Emit started event immediately so the client knows the connection
        #    is established even if the job has zero log rows yet.
        yield format_sse_event(
            event="started",
            id=f"started:{job_id}",
            data={"job_id": job_id, "ts": _now_iso()},
        )
        has_emitted_started = True

        # 2. Replay historical logs. Paginated to avoid OOM per Pushback 6.
        history_cap = getattr(config, "SSE_MAX_HISTORY_ROWS", 5000)
        history_emitted = 0
        while history_emitted < history_cap:
            rows = conn.execute(
                "SELECT event_id, timestamp, level, tag, status_state, message, payload_json "
                "FROM logs WHERE job_id = ? AND event_id > ? "
                "ORDER BY event_id ASC LIMIT 200",
                (job_id, last_seen_id),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                last_seen_id = row["event_id"]
                phase = derive_phase(row["status_state"])
                yield format_sse_event(
                    event=phase,
                    id=row["event_id"],
                    data=_row_to_event_data(row),
                )
                last_phase = phase
                history_emitted += 1
                # If the historical replay hits a terminal status, the job is
                # already done — emit completed with the final row and return.
                if is_terminal(row["status_state"]):
                    yield format_sse_event(
                        event="completed",
                        id=f"completed:{row['event_id']}",
                        data={"job_id": job_id, "final_status_state": row["status_state"]},
                    )
                    return
                if phase == "paused":
                    # Historical replay shows the job is paused. Emit paused
                    # and return — client reconnects after /resume.
                    yield format_sse_event(
                        event="paused",
                        id=f"paused:{row['event_id']}",
                        data={"job_id": job_id, "reason": "historical pause detected"},
                    )
                    return
            if len(rows) < 200:
                break

        # 3. Live tail loop.
        poll_fallback = getattr(config, "SSE_POLL_FALLBACK_SECONDS", 1.0)
        while True:
            # Check for shutdown FIRST so we exit cleanly even if new logs
            # arrived simultaneously.
            if shutdown.is_shutting_down():
                yield format_sse_event(
                    event="completed",
                    id=f"shutdown:{job_id}",
                    data={"job_id": job_id, "reason": "server_shutting_down"},
                    comment="server shutting down",
                )
                return

            # Drain any new rows.
            new_rows_found = False
            while True:
                rows = conn.execute(
                    "SELECT event_id, timestamp, level, tag, status_state, message, payload_json "
                    "FROM logs WHERE job_id = ? AND event_id > ? "
                    "ORDER BY event_id ASC LIMIT 200",
                    (job_id, last_seen_id),
                ).fetchall()
                if not rows:
                    break
                new_rows_found = True
                for row in rows:
                    last_seen_id = row["event_id"]
                    phase = derive_phase(row["status_state"])
                    yield format_sse_event(
                        event=phase,
                        id=row["event_id"],
                        data=_row_to_event_data(row),
                    )
                    last_phase = phase
                    if is_terminal(row["status_state"]):
                        yield format_sse_event(
                            event="completed",
                            id=f"completed:{row['event_id']}",
                            data={"job_id": job_id, "final_status_state": row["status_state"]},
                        )
                        return
                    if phase == "paused":
                        yield format_sse_event(
                            event="paused",
                            id=f"paused:{row['event_id']}",
                            data={"job_id": job_id, "reason": "HITL pause"},
                        )
                        return

            # Wait for either: new log notification, shutdown, or fallback poll.
            # Reset the notify event so we block again until next poke.
            notify_event.clear()
            wait_tasks = [
                asyncio.create_task(notify_event.wait()),
                asyncio.create_task(shutdown.wait_for_shutdown(timeout=poll_fallback)),
            ]
            done, pending = await asyncio.wait(
                wait_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for p in pending:
                p.cancel()
                try:
                    await p
                except (asyncio.CancelledError, Exception):
                    pass
    except asyncio.CancelledError:
        # Client disconnected. Per FastAPI SSE docs, the generator cannot be
        # cancelled cleanly mid-yield; just return.
        # Ref: https://fastapi.tiangolo.com/tutorial/server-sent-events/
        return
    except Exception as e:
        yield format_sse_event(
            event="error",
            id=f"error:{job_id}",
            data={"job_id": job_id, "error": str(e)},
        )
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass
        log_notify.clear(job_id)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
