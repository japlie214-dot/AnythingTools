# utils/sse/broker.py
"""SSE Broker: per-subscriber asyncio.Queue fan-out with logs.db tailing.

The broker bridges the synchronous database writer (which runs in a
dedicated thread) and the asynchronous SSE endpoints (which run in the
FastAPI event loop).

Design:
  - Each SSE endpoint calls ``subscribe(job_id)`` and receives an
    ``asyncio.Queue``.
  - The tailer coroutine polls logs.db for new entries and publishes
    ``LogEntryEvent`` / ``JobStatusEvent`` to all subscribers for that job.
  - When a job reaches terminal state, the tailer publishes a
    ``StreamEndEvent`` and the endpoint closes.
  - Slow consumers (queue full) are disconnected to prevent memory
    exhaustion.

Concurrency notes:
  - asyncio.Queue is NOT thread-safe. The tailer runs in the event
    loop, so it can use put_nowait safely.
  - The logs.db writer thread is separate and does not interact with
    the queues directly — the tailer polls the DB.
  - Ref: https://docs.python.org/3/library/asyncio-queue.html

SSE wire format:
  - Each event is serialized as ``data: <json>\n\n``
  - Comment lines (``: ping\n\n``) are sent every 15s to prevent
    proxy idle-timeout closes.
  - Ref: https://html.spec.whatwg.org/multipage/server-sent-events.html#authoring-notes
"""

from __future__ import annotations
import asyncio
import json
import time
from typing import Any, Optional
from collections import defaultdict
from datetime import datetime, timezone

import config
from utils.logger.core import get_dual_logger
from utils.id_generator import ULID
from utils.sse.events import (
    SSEEvent, JobStatusEvent, LogEntryEvent, ToolProgressEvent,
    ToolCompletedEvent, JobFailedEvent, StreamEndEvent,
)

log = get_dual_logger(__name__)

# Terminal job statuses that trigger stream.end
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "ABANDONED", "PARTIAL", "SKIPPED"}


class SSEBroker:
    """Manages SSE subscriptions and event fan-out per job_id."""

    def __init__(self) -> None:
        # Map: job_id -> set of asyncio.Queue
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._tailer_task: Optional[asyncio.Task] = None
        self._last_polled_ts: dict[str, str] = {}  # job_id -> last seen timestamp

    async def start_tailer(self) -> None:
        """Start the background logs.db tailer coroutine."""
        if self._tailer_task is not None and not self._tailer_task.done():
            return
        self._tailer_task = asyncio.create_task(self._tail_loop(), name="sse-tailer")

    async def stop_tailer(self) -> None:
        """Stop the tailer coroutine (graceful shutdown)."""
        if self._tailer_task is not None:
            self._tailer_task.cancel()
            try:
                await self._tailer_task
            except asyncio.CancelledError:
                pass
            self._tailer_task = None

    async def subscribe(self, job_id: str) -> asyncio.Queue:
        """Subscribe to SSE events for a job.

        Returns an asyncio.Queue from which the caller can ``await get()``
        to receive SSEEvent instances. The caller must call
        ``unsubscribe(job_id, queue)`` when done.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=config.SSE_SUBSCRIBER_QUEUE_MAXSIZE)
        async with self._lock:
            self._subscribers[job_id].add(queue)
            subscriber_count = len(self._subscribers[job_id])

        # 5W+1H structured log for observability
        log.dual_log(
            tag="SSE:Connect",
            message=f"SSE subscriber connected for job {job_id}",
            payload={
                "who": f"subscriber:{ULID.generate()}",
                "what": "sse_subscribe",
                "when": datetime.now(timezone.utc).isoformat(),
                "where": f"job:{job_id}",
                "why": "client_requested_stream",
                "how": "asyncio.Queue",
                "job_id": job_id,
                "active_subscribers_for_job": subscriber_count,
                "queue_maxsize": config.SSE_SUBSCRIBER_QUEUE_MAXSIZE,
            },
        )

        # Emit a synthetic job.status_changed event with the current status
        # so late subscribers know the job's state immediately.
        current_status = await self._get_job_status(job_id)
        if current_status:
            await self._publish(job_id, JobStatusEvent(job_id=job_id, status=current_status))

        return queue

    async def unsubscribe(self, job_id: str, queue: asyncio.Queue, reason: str = "client_disconnect") -> None:
        """Unsubscribe from SSE events for a job."""
        async with self._lock:
            self._subscribers[job_id].discard(queue)
            subscriber_count = len(self._subscribers[job_id])
            if subscriber_count == 0:
                del self._subscribers[job_id]

        log.dual_log(
            tag="SSE:Disconnect",
            message=f"SSE subscriber disconnected for job {job_id}",
            payload={
                "who": f"subscriber:{id(queue)}",
                "what": "sse_unsubscribe",
                "when": datetime.now(timezone.utc).isoformat(),
                "where": f"job:{job_id}",
                "why": reason,
                "how": "asyncio.Queue.discard",
                "job_id": job_id,
                "remaining_subscribers_for_job": subscriber_count,
            },
        )

    async def _publish(self, job_id: str, event: SSEEvent) -> None:
        """Publish an event to all subscribers of a job.

        Slow consumers (queue full) are disconnected to prevent memory
        exhaustion. This is the recommended backpressure strategy per
        the asyncio.Queue docs:
        https://docs.python.org/3/library/asyncio-queue.html
        """
        async with self._lock:
            subscribers = list(self._subscribers.get(job_id, set()))

        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer — disconnect with a structured 5W+1H log.
                log.dual_log(
                    tag="SSE:Drop",
                    level="WARNING",
                    message=f"SSE subscriber dropped (queue full) for job {job_id}",
                    payload={
                        "who": f"subscriber:{id(queue)}",
                        "what": "sse_drop_slow_consumer",
                        "when": datetime.now(timezone.utc).isoformat(),
                        "where": f"job:{job_id}",
                        "why": "queue_full_backpressure",
                        "how": "QueueFull exception on put_nowait",
                        "job_id": job_id,
                        "queue_maxsize": config.SSE_SUBSCRIBER_QUEUE_MAXSIZE,
                    },
                )
                await self.unsubscribe(job_id, queue, reason="slow_consumer_dropped")

    async def _get_job_status(self, job_id: str) -> Optional[str]:
        """Read the current status of a job from the operational DB."""
        try:
            from database.connection import DatabaseManager
            conn = DatabaseManager.get_read_connection()
            row = conn.execute(
                "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return row["status"] if row else None
        except Exception:
            return None

    async def _tail_loop(self) -> None:
        """Background coroutine that polls logs.db for new entries.

        Polls every 0.5s. For each subscribed job_id, queries logs.db
        for entries with timestamp > last_polled_ts and publishes them
        as LogEntryEvent / JobStatusEvent / ToolProgressEvent.

        When a job reaches terminal state, publishes a StreamEndEvent
        and cleans up subscribers.
        """
        poll_interval = 0.5  # seconds
        while True:
            try:
                async with self._lock:
                    active_job_ids = list(self._subscribers.keys())

                if not active_job_ids:
                    await asyncio.sleep(poll_interval)
                    continue

                for job_id in active_job_ids:
                    await self._poll_job_logs(job_id)

                    # Check for terminal state
                    status = await self._get_job_status(job_id)
                    if status in TERMINAL_STATUSES:
                        await self._publish(job_id, StreamEndEvent(
                            job_id=job_id, reason="terminal_state"
                        ))
                        # Also emit a JobStatusEvent for the terminal state
                        await self._publish(job_id, JobStatusEvent(
                            job_id=job_id, status=status
                        ))
                        # Clean up all subscribers for this job
                        async with self._lock:
                            queues_to_remove = list(self._subscribers.get(job_id, set()))
                        for q in queues_to_remove:
                            await self.unsubscribe(job_id, q, reason="terminal_state_reached")

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.dual_log(
                    tag="SSE:Tailer:Error",
                    level="ERROR",
                    message=f"SSE tailer error: {e}",
                    payload={
                        "who": "sse_tailer",
                        "what": "tail_loop_error",
                        "when": datetime.now(timezone.utc).isoformat(),
                        "where": "utils/sse/broker.py:_tail_loop",
                        "why": str(e),
                        "how": "exception_caught",
                        "error": str(e),
                    },
                )
                await asyncio.sleep(poll_interval)

    async def _poll_job_logs(self, job_id: str) -> None:
        """Poll logs.db for new entries for a specific job."""
        last_ts = self._last_polled_ts.get(job_id)
        try:
            from database.connection import LogsDatabaseManager
            conn = LogsDatabaseManager.get_read_connection()

            if last_ts:
                rows = conn.execute(
                    "SELECT timestamp, level, tag, status_state, message, payload_json, event_id "
                    "FROM logs WHERE job_id = ? AND timestamp > ? ORDER BY timestamp ASC",
                    (job_id, last_ts)
                ).fetchall()
            else:
                # First poll for this job — get the last 50 entries to provide
                # immediate context to late subscribers.
                rows = conn.execute(
                    "SELECT timestamp, level, tag, status_state, message, payload_json, event_id "
                    "FROM logs WHERE job_id = ? ORDER BY timestamp DESC LIMIT 50",
                    (job_id,)
                ).fetchall()
                rows = list(reversed(rows))  # Reverse to chronological order

            for r in rows:
                ts = r["timestamp"]
                if not last_ts or ts > last_ts:
                    self._last_polled_ts[job_id] = ts
                    last_ts = ts

                # Parse payload_json
                payload = None
                if r["payload_json"]:
                    try:
                        payload = json.loads(r["payload_json"])
                    except Exception:
                        payload = None

                # Publish as LogEntryEvent
                await self._publish(job_id, LogEntryEvent(
                    job_id=job_id,
                    level=r["level"],
                    tag=r["tag"] or "",
                    message=r["message"] or "",
                    payload=payload,
                    log_timestamp=ts,
                ))

                # If this log entry carries a status_state, also publish a JobStatusEvent
                if r["status_state"]:
                    await self._publish(job_id, JobStatusEvent(
                        job_id=job_id, status=r["status_state"]
                    ))

        except Exception as e:
            log.dual_log(
                tag="SSE:Poll:Error",
                level="WARNING",
                message=f"Failed to poll logs for job {job_id}: {e}",
                payload={"job_id": job_id, "error": str(e)},
            )


# Module-level singleton
_global_broker: Optional[SSEBroker] = None


def get_global_broker() -> SSEBroker:
    """Get the singleton SSEBroker instance."""
    global _global_broker
    if _global_broker is None:
        _global_broker = SSEBroker()
    return _global_broker
