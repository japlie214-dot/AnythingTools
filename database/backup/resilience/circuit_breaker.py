# database/backup/resilience/circuit_breaker.py
import time
from typing import Callable, Any
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class CircuitOpenError(Exception):
    pass

class CircuitBreaker:
    CLOSED = 'CLOSED'
    OPEN = 'OPEN'
    HALF_OPEN = 'HALF_OPEN'

    def __init__(self, failure_threshold: int = 3, reset_timeout: int = 300):
        self.state = self.CLOSED
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.failure_count = 0
        self.last_failure_time = 0.0

    def call(self, func: Callable, *args, **kwargs) -> Any:
        if self.state == self.OPEN:
            if time.monotonic() - self.last_failure_time > self.reset_timeout:
                self.state = self.HALF_OPEN
                log.dual_log(tag="Resilience:CircuitBreaker:HalfOpen", message="Circuit breaker transitioning to HALF_OPEN", level="INFO", payload={"state": "HALF_OPEN", "failure_count": self.failure_count})
            else:
                raise CircuitOpenError("Circuit breaker is OPEN")
        try:
            result = func(*args, **kwargs)
            if self.state == self.HALF_OPEN:
                self.state = self.CLOSED
                self.failure_count = 0
                log.dual_log(tag="Resilience:CircuitBreaker:Closed", message="Circuit breaker recovered and CLOSED", level="INFO", payload={"state": "CLOSED", "recovered": True})
            return result
        except Exception as e:
            self.failure_count += 1
            self.last_failure_time = time.monotonic()
            if self.failure_count >= self.failure_threshold:
                self.state = self.OPEN
                log.dual_log(tag="Resilience:CircuitBreaker:Open", message="Circuit breaker OPENED", level="CRITICAL", payload={"error": str(e)})
            raise
