# utils/telegram/rate_limiter.py
import asyncio
import time
import threading
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class GlobalRateLimiter:
    """Thread-safe, global rate limiter implementing a 'wait and block' strategy."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(GlobalRateLimiter, cls).__new__(cls)
            cls._instance._last_send_time = {}
            cls._instance._global_last_send = 0.0
            cls._instance._lock = threading.Lock()
        return cls._instance

    async def wait_and_block(self, chat_id: str, delay_same_chat: float = 3.1):
        """Blocks the current execution until it is safe to send a message."""
        now = time.monotonic()
        sleep_time = 0.0

        with self._lock:
            earliest_global = self._global_last_send + 0.05
            earliest_chat = self._last_send_time.get(chat_id, 0.0) + delay_same_chat
            
            target_send_time = max(now, earliest_global, earliest_chat)
            
            self._global_last_send = target_send_time
            self._last_send_time[chat_id] = target_send_time
            
            sleep_time = target_send_time - now

        if sleep_time > 0:
            log.dual_log(tag="Telegram:RateLimiter", message=f"Throttling chat {chat_id} for {sleep_time:.2f}s")
            await asyncio.sleep(sleep_time)
