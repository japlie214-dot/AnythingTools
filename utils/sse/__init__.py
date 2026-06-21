# utils/sse/__init__.py
"""Server-Sent Events (SSE) broker and event models.

The SSE broker provides per-subscriber asyncio.Queue fan-out for real-time
job progress streaming. It bridges the synchronous database writer
(logs_enqueue_write) and the asynchronous FastAPI SSE endpoints.

Architecture:
  - Producers (worker, tools) write to logs.db via logs_enqueue_write.
  - A single SSE tailer coroutine polls logs.db for new entries and
    publishes them to the SSEBroker.
  - The SSEBroker maintains a set of per-subscriber asyncio.Queues.
  - SSE endpoints subscribe to the broker and yield events to clients.

Ref: https://html.spec.whatwg.org/multipage/server-sent-events.html
     https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse
"""

from utils.sse.broker import SSEBroker, get_global_broker
from utils.sse.events import SSEEvent, JobStatusEvent, LogEntryEvent, ToolProgressEvent, ToolCompletedEvent, JobFailedEvent, StreamEndEvent

__all__ = [
    "SSEBroker",
    "get_global_broker",
    "SSEEvent",
    "JobStatusEvent",
    "LogEntryEvent",
    "ToolProgressEvent",
    "ToolCompletedEvent",
    "JobFailedEvent",
    "StreamEndEvent",
]
