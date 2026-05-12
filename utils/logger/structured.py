# utils/logger/structured.py
import time
from contextlib import contextmanager
from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)

@contextmanager
def granular_log(tag: str, **inputs):
    if not inputs:
        inputs = {"info": "No input metadata provided"}
    
    entry_payload = {**inputs, "lifecycle_state": "Entry"}
    log.dual_log(tag=tag, message=f"Entering {tag}", level="DEBUG", payload=entry_payload)
    start = time.monotonic()
    try:
        yield
        dur = time.monotonic() - start
        log.dual_log(tag=tag, message=f"Exiting {tag}", level="DEBUG", payload={"lifecycle_state": "Exit", "duration_s": dur})
    except Exception as e:
        dur = time.monotonic() - start
        log.dual_log(tag=tag, message=f"Error in {tag}", level="ERROR", payload={"lifecycle_state": "Error", "duration_s": dur}, exc_info=e)
        raise
