# utils/logger/validation.py
import re
from typing import Optional

_TAG_PATTERN = re.compile(r'^[A-Za-z][A-Za-z0-9_]*:[A-Za-z][A-Za-z0-9_]*:[A-Za-z][A-Za-z0-9_]*$')

def validate_tag(tag: str, caller_name: str = "") -> bool:
    if not tag or not isinstance(tag, str): return False
    if _TAG_PATTERN.match(tag): return True
    try:
        from utils.logger.core import get_dual_logger
        get_dual_logger("logger.validation").dual_log(
            tag="Logger:Contract:TagViolation",
            message=f"Tag '{tag}' violates Category:Sub-Category:Action format",
            level="WARNING",
            payload={"invalid_tag": tag, "caller": caller_name, "rule": "Tags must be 3-part identifiers"}
        )
    except Exception: pass
    return False
