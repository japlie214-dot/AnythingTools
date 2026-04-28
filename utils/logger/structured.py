# utils/logger/structured.py
import time
from contextlib import contextmanager
from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)

@contextmanager
def granular_log(tag: str, **inputs):
    log.dual_log(tag=f"{tag}:Entry", message=f"Entering {tag}", level="DEBUG", payload=inputs)
    start = time.monotonic()
    try:
        yield
        dur = time.monotonic() - start
        log.dual_log(tag=f"{tag}:Exit", message=f"Exiting {tag}", level="DEBUG", payload={"duration_s": dur})
    except Exception as e:
        dur = time.monotonic() - start
        log.dual_log(tag=f"{tag}:Error", message=f"Error in {tag}", level="ERROR", payload={"duration_s": dur}, exc_info=e)
        raise