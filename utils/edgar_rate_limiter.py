# utils/edgar_rate_limiter.py
import time
import threading
from collections import deque
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class EdgarRateLimiter:
    """Synchronous sliding-window rate limiter for SEC EDGAR API (10 req/sec limit)."""
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
        self._max_rate = 8  # Conservative limit below SEC's 10/sec
        self._time_period = 1.0
        self._timestamps = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block the current thread until it's safe to make an EDGAR request."""
        with self._lock:
            now = time.monotonic()
            
            while self._timestamps and self._timestamps[0] < now - self._time_period:
                self._timestamps.popleft()

            if len(self._timestamps) >= self._max_rate:
                sleep_time = (self._timestamps[0] + self._time_period) - now
                if sleep_time > 0:
                    log.dual_log(
                        tag="Edgar:RateLimiter:Throttle",
                        message=f"Throttling EDGAR request for {sleep_time:.2f}s",
                        level="DEBUG",
                        payload={"sleep_time_s": round(sleep_time, 3)}
                    )
                    time.sleep(sleep_time)
            
            self._timestamps.append(time.monotonic())

edgar_limiter = EdgarRateLimiter()
