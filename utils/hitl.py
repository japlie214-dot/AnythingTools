# utils/hitl.py
"""Human-in-the-loop (HITL) helpers for browser-capable tools.

This module standardizes how tools pause for human intervention and
provides consistent cancellation semantics (typing "Stop" at the prompt).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

from utils.browser_lock import browser_lock
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


def hitl_wait_for_operator(
    cancellation_flag: threading.Event,
    message: str = "Resolve the issue in the browser, then press ENTER to resume...\n",
    timeout: Optional[float] = None,
) -> bool:
    """Synchronously wait for operator input while holding browser_lock.

    Returns True if resumed by ENTER, False if the operator typed `Stop`.
    The cancellation_flag will be set if the operator types `Stop`.
    """
    if not browser_lock.locked():
        raise RuntimeError("hitl_wait_for_operator must be called with browser_lock held")

    # Delegate blocking input() to a thread so the asyncio loop (if present) remains responsive.
    try:
        user_input = input(message)
    except EOFError:
        user_input = ""

    if user_input.strip().lower() == "stop":
        cancellation_flag.set()
        log.dual_log(
            tag="HITL:Cancel",
            message="Operator typed 'Stop' — cancellation_flag set.",
            status_state="CANCELLED",
            notify_user=True,
        )
        return False

    return True


async def hitl_wait_for_operator_async(
    cancellation_flag: threading.Event,
    message: str = "Resolve the issue in the browser, then press ENTER to resume...\n",
    timeout: Optional[float] = None,
) -> bool:
    """Async variant: same semantics, but uses asyncio.to_thread for input."""
    if not browser_lock.locked():
        raise RuntimeError("hitl_wait_for_operator_async must be called with browser_lock held")

    # Async-friendly blocking input (non-blocking to event loop)
    try:
        user_input = await asyncio.to_thread(input, message)
    except EOFError:
        user_input = ""

    if user_input.strip().lower() == "stop":
        cancellation_flag.set()
        log.dual_log(
            tag="HITL:Cancel",
            message="Operator typed 'Stop' — cancellation_flag set.",
            status_state="CANCELLED",
            notify_user=True,
        )
        return False

    return True


def mark_paused_for_hitl(
    tag: str,
    message: str,
    payload: dict | None = None,
) -> None:
    """Centralized logging for PAUSED_FOR_HITL with user notification."""
    log.dual_log(
        tag=tag,
        message=message,
        status_state="PAUSED_FOR_HITL",
        payload=payload,
        level="WARNING",
        notify_user=True,
    )


def pause_for_hitl(message: str) -> None:
    """Blocks the worker thread until the operator resolves the challenge.

    Important: Do NOT release the shared browser_lock here. Holding the lock
    prevents other worker threads from hijacking the browser while the operator
    interacts with the UI. This function intentionally performs only a
    synchronous input() call and returns; the caller retains responsibility for
    lock handling and job lifecycle.
    """
    print(f"\n\n[!!!] HITL ALERT: {message}")
    print(">>> Solve the CAPTCHA/Blocker in the browser window.")
    print(">>> Press ENTER to resume, or type 'cancel' to kill the job.")

    try:
        user_input = input("Decision: ").strip().lower()
    except EOFError:
        user_input = "cancel"

    if user_input == "cancel":
        raise Exception("USER_CANCELLED: Job terminated by operator.")
