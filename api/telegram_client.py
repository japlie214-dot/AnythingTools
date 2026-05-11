# api/telegram_client.py
"""Legacy Telegram client removed.

This module previously provided TelegramBot wrapper. It has been removed to
eradicate legacy notifier shims. A minimal compatibility stub is kept so any
accidental imports do not crash the process; the stub methods are no-ops and
log deprecation notices via the structured dual logger.
"""
from typing import Optional

from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class TelegramBot:
    """Compatibility stub for legacy API imports.

    NOTE: This stub intentionally does not initialize or use the
    python-telegram-bot library. Use utils.telegram.* implementations for
    production publisher workflows.
    """
    _instance: Optional[object] = None
    _chat_id: Optional[str] = None

    @classmethod
    def get_bot(cls) -> object:
        raise RuntimeError("api.telegram_client has been removed. Use utils.telegram.* implementations instead.")

    @classmethod
    def get_chat_id(cls) -> Optional[str]:
        return None

    @classmethod
    def set_chat_id(cls, chat_id: str):
        cls._chat_id = str(chat_id)
        try:
            log.dual_log(tag="Telegram:Bot:Handshake", message=f"Deprecated api.telegram_client.set_chat_id called", payload={"chat_id_last4": str(chat_id)[-4:] if chat_id else None})
        except Exception:
            pass

    @staticmethod
    async def send_chat_message(text: str, parse_mode: str = "HTML") -> bool:
        try:
            log.dual_log(tag="TelegramBot", message="Deprecated send_chat_message called; no-op", payload={"length": len(text) if text else 0})
        except Exception:
            pass
        return False

    @staticmethod
    async def send_poll(*args, **kwargs):
        try:
            log.dual_log(tag="TelegramBot", message="Deprecated send_poll called; no-op", payload={})
        except Exception:
            pass
        return None
