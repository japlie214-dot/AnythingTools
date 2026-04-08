"""
Logging Severity Definitions (locked):
- DEBUG: Low-level tracing and granular variable states. Ignored by Debugger Agent.
- INFO: Standard operational milestones and state changes. Ignored by Debugger Agent.
- WARNING: Recoverable anomalies, failed retries, or degraded modes. Triggers Debugger Agent.
- ERROR: Operation failures preventing a single feature/task from succeeding. Triggers Debugger Agent.
- CRITICAL: System-wide failures requiring immediate intervention. Triggers Debugger Agent.
"""
# utils/logger/__init__.py
# Submodules are imported in strict DAG order so each module's dependencies
# are guaranteed to be initialized before the module that needs them.

import utils.logger.setup      # colorama fix; no intra-package deps
import utils.logger.state      # shared ContextVar and mutable state
import utils.logger.routing    # LOG_MAP, DEBUGGER_FILE_MAP, _LOG_DIR
import utils.logger.formatters # formatters and serialization helpers
import utils.logger.handlers   # handler singletons and exc_info normalization
import utils.logger.core       # SumAnalLogger class and public API functions

from utils.logger.core import (
    get_dual_logger,
    clear_sql_log,
    global_log_purge,
    get_sql_logger,
    flush_all_log_handlers,
)

__all__ = [
    "get_dual_logger",
    "clear_sql_log",
    "global_log_purge",
    "get_sql_logger",
    "flush_all_log_handlers",
]
