# database/backup/engine/type_sanitizer.py
import datetime
from typing import Any, Dict, List
import decimal

from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

_pandas = None
_numpy = None

def _get_pandas():
    global _pandas
    if _pandas is None:
        try:
            import pandas as _pd
            _pandas = _pd
        except ImportError:
            _pandas = False
    return _pandas if _pandas else None

def _get_numpy():
    global _numpy
    if _numpy is None:
        try:
            import numpy as _np
            _numpy = _np
        except ImportError:
            _numpy = False
    return _numpy if _numpy else None

def _sanitize_value(value: Any) -> Any:
    """Convert a single value to a Snowflake-compatible Python type."""
    if value is None:
        return None

    pd = _get_pandas()
    np = _get_numpy()

    if pd is not None and isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.to_pydatetime()

    if pd is not None and pd.isna(value) and not isinstance(value, (float, int)):
        return None

    if np is not None and isinstance(value, np.datetime64):
        return value.astype('datetime64[us]').item()

    if np is not None and isinstance(value, np.timedelta64):
        return float(value / np.timedelta64(1, 's'))

    if np is not None and isinstance(value, (np.integer,)):
        return int(value)

    if np is not None and isinstance(value, (np.floating,)):
        if np.isnan(value) or np.isinf(value):
            return None
        return float(value)
        
    if isinstance(value, decimal.Decimal):
        return float(value)

    return value

def sanitize_snowflake_params(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sanitize a list of record dicts for Snowflake parameter binding."""
    if not records:
        return records

    sanitized = []
    for record in records:
        clean = {}
        for key, value in record.items():
            clean[key] = _sanitize_value(value)
        sanitized.append(clean)

    return sanitized
