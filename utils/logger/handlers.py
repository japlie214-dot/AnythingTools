# utils/logger/handlers.py
import logging
import logging.handlers
import sys
import threading
from typing import Any

from utils.logger.formatters import ConsoleFormatter, FileFormatter, PayloadOrErrorFilter
from utils.logger.routing import LOG_MAP, _LOG_DIR
from utils.logger.state import _log_config

# _handler_cache and _cache_lock are defined here and NOT in state.py because
# they guard handler object lifecycle (creation, closure, removal), which is
# exclusively a handlers.py concern. core.py imports both symbols from here.
_handler_cache: dict[str, logging.Handler] = {}
_cache_lock = threading.RLock()


def _get_master_handlers() -> tuple[logging.StreamHandler]:
    """Return the shared console_handler, creating on first call."""
    with _cache_lock:
        if "master_console" not in _handler_cache:
            hc = logging.StreamHandler()
            hc.setFormatter(ConsoleFormatter())
            console_level = getattr(
                logging,
                getattr(_log_config, "LOG_CONSOLE_LEVEL", "INFO").upper(),
                logging.INFO,
            ) if _log_config else logging.INFO
            hc.setLevel(console_level)
            _handler_cache["master_console"] = hc

        return (_handler_cache["master_console"],)


def _get_specialized_handler(destination: str) -> None:
    return None


def _normalize_exc_info(exc_info: Any) -> tuple | None:
    """Normalize True / exception instance / tuple → (type, value, tb) or None."""
    if not exc_info:
        return None
    if isinstance(exc_info, BaseException):
        return (type(exc_info), exc_info, exc_info.__traceback__)
    if isinstance(exc_info, tuple):
        return exc_info
    return sys.exc_info()
