import asyncio
import html
from datetime import datetime
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError, RetryAfter
from utils.logger import get_dual_logger
import config

log = get_dual_logger(__name__)

class TelegramBot:
    _instance: Optional[Bot] = None
    _chat_id: Optional[str] = None

    @classmethod
    def get_bot(cls) -> Bot:
        if cls._instance is None:
            token = config.TELEGRAM_BOT_TOKEN
            if not token:
                raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")
            cls._instance = Bot(token=token)
        return cls._instance

    @classmethod
    def get_chat_id(cls) -> Optional[str]:
        return cls._chat_id

    @classmethod
    def set_chat_id(cls, chat_id: str):
        cls._chat_id = str(chat_id)
        log.dual_log(tag="TelegramBot:Handshake", message=f"Bound to chat ID {chat_id}")

    @staticmethod
    async def send_chat_message(text: str, parse_mode: str = "HTML") -> bool:
        chat_id = TelegramBot.get_chat_id()
        if not config.TELEGRAM_BOT_TOKEN or not chat_id:
            return False

        bot = TelegramBot.get_bot()
        timestamp = datetime.now().strftime("%H:%M:%S")
        # When using HTML parse mode, we trust the caller has provided safe HTML
        # (e.g., for artifact deep links). We prepend timestamp safely.
        safe_text = f"<b>[{timestamp}]</b> {text}"

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=safe_text,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
            return True
        except RetryAfter as e:
            log.dual_log(tag="TelegramBot", message=f"Rate limited. Sleeping {e.retry_after}s", level="WARNING")
            await asyncio.sleep(e.retry_after)
            return await TelegramBot.send_chat_message(text, parse_mode)
        except TelegramError as e:
            log.dual_log(tag="TelegramBot", message=f"Failed to send monitoring message: {e}", level="WARNING")
            return False

    @staticmethod
    async def send_poll(question: str, options: list[str], correct_option_id: int, explanation: str = ""):
        chat_id = TelegramBot.get_chat_id()
        if not chat_id:
            return
        bot = TelegramBot.get_bot()
        try:
            await bot.send_poll(
                chat_id=chat_id,
                question=question,
                options=options,
                type="quiz",
                correct_option_id=correct_option_id,
                explanation=explanation,
                is_anonymous=True,
            )
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await TelegramBot.send_poll(question, options, correct_option_id, explanation)
        except TelegramError as e:
            log.dual_log(tag="TelegramBot", message=f"Failed to send poll: {e}", level="WARNING")

    @classmethod
    async def run_orphan_handshake(cls):
        """Long-polling background task to capture the first message and bind chat_id."""
        if not config.TELEGRAM_BOT_TOKEN:
            return
        bot = cls.get_bot()
        offset = 0
        log.dual_log(tag="TelegramBot:Handshake", message="Listening for orphan handshake (send any message to the bot)...")
        while cls._chat_id is None:
            try:
                updates = await bot.get_updates(offset=offset, timeout=30)
                for update in updates:
                    offset = update.update_id + 1
                    if update.effective_chat:
                        cls.set_chat_id(update.effective_chat.id)
                        await bot.send_message(
                            chat_id=cls._chat_id,
                            text="✅ Handshake Complete. Monitoring Job Stream."
                        )
                        return
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.dual_log(tag="TelegramBot:Handshake", message=f"Handshake poll error: {e}", level="WARNING")
                await asyncio.sleep(5)
