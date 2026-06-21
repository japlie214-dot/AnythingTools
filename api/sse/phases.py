# api/sse/phases.py
"""Derive SSE phase from logs.status_state.

Per Pushback 2: derive EXCLUSIVELY from logs.db.status_state, not jobs.status.
The two are written via separate queues (logs_enqueue_write at
utils/logger/core.py:138 vs enqueue_write at utils/logger/core.py:147) with
no cross-queue ordering guarantee. logs.db is the canonical source because
it is single-queue and monotonic by event_id (a ULID).
"""

# Terminal statuses that cause the SSE projector to close the stream after
# emitting the `completed` event. Sourced from database/schemas/jobs.py:10.
TERMINAL_LOG_STATES = frozenset({
    "COMPLETED",
    "PARTIAL",
    "FAILED",
    "ABANDONED",
    "SKIPPED",
    "CANCELLED",
    "CANCELLING",
    "INTERRUPTED",
})


def derive_phase(status_state: str | None) -> str:
    """Map a logs.status_state value to an SSE phase.

    Returns one of: "started", "running", "paused", "completed", "error".
    - None or empty -> "running" (default for live log lines without status)
    - "RUNNING" -> "running"
    - "PAUSED_FOR_HITL" -> "paused"
    - Terminal states -> "completed"
    - Anything else -> "running" (forward-compat: unknown states treated as
      live logs, not errors, to avoid breaking the stream on new statuses)
    """
    if not status_state:
        return "running"
    s = status_state.upper()
    if s == "RUNNING":
        return "running"
    if s == "PAUSED_FOR_HITL":
        return "paused"
    if s in TERMINAL_LOG_STATES:
        return "completed"
    return "running"


def is_terminal(status_state: str | None) -> bool:
    """True if this status_state ends the SSE stream."""
    if not status_state:
        return False
    return status_state.upper() in TERMINAL_LOG_STATES
