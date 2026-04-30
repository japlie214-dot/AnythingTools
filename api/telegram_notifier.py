# api/telegram_notifier.py
"""
Simple, non-blocking Telegram notifier shim used by AnythingTools API.
This is a light-weight placeholder that can be expanded later to actually
push messages using python-telegram-bot or any other transport. It is kept
separate so the worker/telemetry path can call a single function.
"""

from utils.logger.core import get_dual_logger
import asyncio

log = get_dual_logger(__name__)

from api.telegram_client import TelegramBot

async def send_notification(text: str) -> None:
    """Main entry point — formats as transparent chat window."""
    await TelegramBot.send_chat_message(text)

async def notify_user(text: str) -> None:
    """Backwards-compatible entry point for legacy callers."""
    await send_notification(f"📢 {text}")

def notify_user_sync(text: str) -> None:
    """Synchronous wrapper for convenience in threaded contexts."""
    try:
        loop = asyncio.get_event_loop()
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(notify_user(text), loop)
        else:
            asyncio.run(notify_user(text))
    except Exception:
        try:
            log.dual_log(tag="API:Notifier", message=f"notify_user_sync fallback: {text}", payload={"text_len": len(text) if text else 0, "text_preview": text[:200]})
        except Exception:
            pass
