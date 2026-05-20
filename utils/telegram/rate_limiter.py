import asyncio
import time
from collections import deque
from typing import Dict
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class SlidingWindowRateLimiter:
    """Async-safe sliding-window rate limiter using future-timestamp reservations."""
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        import config as _cfg
        self._overall_max_rate = getattr(_cfg, 'TELEGRAM_RATELIMIT_OVERALL_MAX', 28)
        self._overall_time_period = getattr(_cfg, 'TELEGRAM_RATELIMIT_OVERALL_PERIOD', 1.0)
        self._group_max_rate = getattr(_cfg, 'TELEGRAM_RATELIMIT_GROUP_MAX', 18)
        self._group_time_period = getattr(_cfg, 'TELEGRAM_RATELIMIT_GROUP_PERIOD', 60.0)

        self._overall_timestamps: deque[float] = deque()
        self._group_timestamps: Dict[str, deque[float]] = {}
        self._frozen_until: float = 0.0
        self._lock = asyncio.Lock()

    async def wait_and_block(self, chat_id: str, is_group: bool = True, delay_same_chat: float | None = None) -> None:
        async with self._lock:
            now = time.monotonic()
            
            # Prune stale timestamps (relative to actual current time)
            while self._overall_timestamps and self._overall_timestamps[0] < now - self._overall_time_period:
                self._overall_timestamps.popleft()
                
            if is_group:
                if chat_id not in self._group_timestamps:
                    self._group_timestamps[chat_id] = deque()
                else:
                    while self._group_timestamps[chat_id] and self._group_timestamps[chat_id][0] < now - self._group_time_period:
                        self._group_timestamps[chat_id].popleft()

            target = now

            if len(self._overall_timestamps) >= self._overall_max_rate:
                target = max(target, self._overall_timestamps[-self._overall_max_rate] + self._overall_time_period)
                
            if is_group and len(self._group_timestamps[chat_id]) >= self._group_max_rate:
                target = max(target, self._group_timestamps[chat_id][-self._group_max_rate] + self._group_time_period)
                
            if not is_group and len(self._overall_timestamps) > 0:
                target = max(target, self._overall_timestamps[-1] + 1.0)

            if self._frozen_until > target:
                target = self._frozen_until

            # Reserve the target slot to prevent concurrent race conditions
            self._overall_timestamps.append(target)
            if is_group:
                self._group_timestamps[chat_id].append(target)

            sleep_time = target - now

        if sleep_time > 0:
            log.dual_log(
                tag="Telegram:RateLimiter:Throttle",
                message=f"Throttling chat {chat_id} for {sleep_time:.2f}s",
                payload={"chat_id": chat_id, "sleep_time_s": round(sleep_time, 3), "is_group": is_group}
            )
            await asyncio.sleep(sleep_time)

    def record_retry_after(self, retry_after: float) -> None:
        self._frozen_until = time.monotonic() + retry_after + 0.1
        log.dual_log(
            tag="Telegram:RateLimiter:RetryAfter",
            message=f"Rate limiter frozen for {retry_after + 0.1:.1f}s",
            level="WARNING",
            payload={"retry_after_s": retry_after, "reason": "telegram_429"}
        )

    def get_stats(self) -> Dict:
        now = time.monotonic()
        active_overall = sum(1 for t in self._overall_timestamps if t > now - self._overall_time_period)
        return {
            "active_overall": active_overall,
            "tracked_groups": len(self._group_timestamps),
            "frozen_remaining": max(0.0, self._frozen_until - now)
        }

GlobalRateLimiter = SlidingWindowRateLimiter
