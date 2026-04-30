# utils/telegram/telegram_client.py
import asyncio
from typing import Optional
from telegram import Bot, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from utils.telegram.types import TelegramErrorInfo
from utils.telegram.rate_limiter import GlobalRateLimiter
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class TelegramAPIClient:
    """Unified Telegram API client using python-telegram-bot >= 22.7."""
    
    def __init__(self, bot_token: str, max_retry_after: int = 120, message_delay: float = 3.1):
        self._bot = Bot(token=bot_token) if bot_token else None
        self.max_retry_after = max_retry_after
        self.message_delay = message_delay
        self.rate_limiter = GlobalRateLimiter()

    async def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: Optional[str] = ParseMode.MARKDOWN_V2,
        disable_link_preview: bool = False
    ) -> TelegramErrorInfo:
        
        if not self._bot:
            return TelegramErrorInfo(success=False, is_permanent=True, description="No bot token configured")
            
        link_preview = LinkPreviewOptions(is_disabled=disable_link_preview)
        retries = 0
        max_retries = 3

        while retries < max_retries:
            await self.rate_limiter.wait_and_block(chat_id, self.message_delay)
            try:
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    link_preview_options=link_preview
                )
                return TelegramErrorInfo(success=True)
            except RetryAfter as e:
                if e.retry_after > self.max_retry_after:
                    return TelegramErrorInfo(success=False, is_transient=True, retry_after=e.retry_after, description="Extreme rate limit")
                log.dual_log(tag="Telegram:Send:RetryAfter", message=f"PTB caught Rate Limit, sleeping {e.retry_after}s", level="WARNING")
                await asyncio.sleep(e.retry_after)
                retries += 1
            except TelegramError as e:
                error_msg = str(e).lower()
                is_perm = "chat not found" in error_msg or "blocked" in error_msg or "forbidden" in error_msg
                log.dual_log(tag="Telegram:Send:Error", message=f"Telegram send error: {e}", level="WARNING", payload={"chat_id": chat_id, "error": str(e)})
                return TelegramErrorInfo(success=False, is_permanent=is_perm, is_transient=not is_perm, description=str(e))
                
        return TelegramErrorInfo(success=False, is_transient=True, description="Max retries exhausted")

    async def close(self):
        if self._bot:
            await self._bot.close()
