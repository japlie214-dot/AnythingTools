"""
Simple, non-blocking Telegram notifier shim used by AnythingTools API.
This is a light-weight placeholder that can be expanded later to actually
push messages using python-telegram-bot or any other transport. It is kept
separate so the worker/telemetry path can call a single function.
"""

from utils.logger.core import get_dual_logger
import asyncio

log = get_dual_logger(__name__)


async def send_notification(text: str) -> None:
    """Send a Telegram message to the operator, splitting long messages safely.

    This uses `smart_split_telegram_message` to avoid breaking Markdown/code-fences
    and sends the resulting chunks sequentially. It respects the configured
    `TELEGRAM_PARSE_MODE` and `TELEGRAM_MESSAGE_MAX_LENGTH` (defaults to 4000).

    Fails silently (logs warnings) on network errors to avoid alert loops.
    """
    import httpx
    import config
    from utils.text_processing import smart_split_telegram_message

    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_USER_ID
    parse_mode = getattr(config, "TELEGRAM_PARSE_MODE", "HTML")
    max_length = getattr(config, "TELEGRAM_MESSAGE_MAX_LENGTH", 4000)

    if not token or not chat_id:
        log.dual_log(
            tag="API:Notifier",
            message="Skipped notification: TELEGRAM_BOT_TOKEN or TELEGRAM_USER_ID not set.",
            level="DEBUG",
        )
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Split into safe chunks using the shared splitter helper
    try:
        chunks = smart_split_telegram_message(text, max_length, parse_mode=parse_mode)
        if not chunks:
            chunks = [text]
    except Exception:
        # If the splitter fails for any reason, fall back to the raw text as one chunk
        chunks = [text]

    try:
        async with httpx.AsyncClient() as client:
            for chunk in chunks:
                payload = {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                }
                try:
                    resp = await client.post(url, json=payload, timeout=5.0)

                    # Handle naive rate-limit retry (Retry-After header may be present)
                    if resp.status_code == 429:
                        ra = resp.headers.get("Retry-After")
                        try:
                            wait = float(ra) if ra else 1.0
                        except Exception:
                            wait = 1.0
                        log.dual_log(tag="API:Notifier", message=f"Rate limited, sleeping {wait}s", level="WARNING")
                        await asyncio.sleep(wait)
                        resp = await client.post(url, json=payload, timeout=5.0)

                    resp.raise_for_status()
                except Exception as e:
                    # Log and continue with remaining chunks — avoid blocking the caller
                    log.dual_log(tag="API:Notifier", message=f"Failed to send push chunk (len={len(chunk)}): {e}", level="WARNING")
    except Exception as e:
        # Top-level client failure
        log.dual_log(tag="API:Notifier", message=f"Failed to send push (client error): {e}", level="WARNING")


async def notify_user(text: str) -> None:
    """Backwards-compatible entry point for legacy callers."""
    await send_notification(text)


def notify_user_sync(text: str) -> None:
    """Synchronous wrapper for convenience in threaded contexts."""
    try:
        # Fire-and-forget the async notifier if the loop is available.
        loop = asyncio.get_event_loop()
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(notify_user(text), loop)
        else:
            # Best-effort: run in a short-lived loop
            asyncio.run(notify_user(text))
    except Exception:
        try:
            log.dual_log(tag="API:Notifier", message=f"notify_user_sync fallback: {text}")
        except Exception:
            pass
