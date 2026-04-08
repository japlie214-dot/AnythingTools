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


def _get_master_handlers() -> tuple[logging.StreamHandler, logging.Handler]:
    """Return the shared (console_handler, master_file_handler) pair, creating on first call."""
    with _cache_lock:
        if "master_console" not in _handler_cache:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            hc = logging.StreamHandler()
            hc.setFormatter(ConsoleFormatter())
            console_level = getattr(
                logging,
                getattr(_log_config, "LOG_CONSOLE_LEVEL", "INFO").upper(),
                logging.INFO,
            ) if _log_config else logging.INFO
            hc.setLevel(console_level)
            _handler_cache["master_console"] = hc

            hf = logging.handlers.TimedRotatingFileHandler(
                _LOG_DIR / "sumanal.txt",
                when="midnight",
                backupCount=30,
                encoding="utf-8",
            )
            hf.setFormatter(FileFormatter())
            hf.addFilter(PayloadOrErrorFilter())
            _handler_cache["master_file"] = hf

        return _handler_cache["master_console"], _handler_cache["master_file"]


def _get_specialized_handler(destination: str) -> logging.FileHandler | None:
    """Return a cached FileHandler for *destination*, creating lazily."""
    if destination not in LOG_MAP:
        return None
    filename = LOG_MAP[destination]
    with _cache_lock:
        if filename not in _handler_cache:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            hf = logging.FileHandler(
                _LOG_DIR / filename, mode="a", encoding="utf-8", delay=False,
            )
            hf.setFormatter(FileFormatter())
            hf.addFilter(PayloadOrErrorFilter())
            _handler_cache[filename] = hf
        return _handler_cache[filename]


def _normalize_exc_info(exc_info: Any) -> tuple | None:
    """Normalize True / exception instance / tuple → (type, value, tb) or None."""
    if not exc_info:
        return None
    if isinstance(exc_info, BaseException):
        return (type(exc_info), exc_info, exc_info.__traceback__)
    if isinstance(exc_info, tuple):
        return exc_info
    return sys.exc_info()
