# api/telegram_notifier.py
"""
Deprecated Telegram notifier shim.

This module used to provide lightweight helpers for pushing notifications to
Telegram. The notifier has been intentionally removed; these no-op wrappers
preserve import compatibility for callers that haven't been migrated yet.
"""

from utils.logger.core import get_dual_logger
import asyncio

log = get_dual_logger(__name__)

async def send_notification(text: str) -> None:
    """Compatibility no-op. Logs a deprecation event."""
    try:
        log.dual_log(tag="API:Notifier:Deprecated", message="send_notification called on deprecated api.telegram_notifier", payload={"text_len": len(text) if text else 0})
    except Exception:
        pass

async def notify_user(text: str) -> None:
    """Backwards-compatible entry point for legacy callers (no-op)."""
    await send_notification(text)


def notify_user_sync(text: str) -> None:
    """Synchronous wrapper that attempts to run the async no-op."""
    try:
        loop = asyncio.get_event_loop()
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(notify_user(text), loop)
        else:
            asyncio.run(notify_user(text))
    except Exception:
        try:
            log.dual_log(tag="API:Notifier:Fallback", message=f"notify_user_sync fallback: {text}", payload={"text_len": len(text) if text else 0, "text_preview": text[:200]})
        except Exception:
            pass
