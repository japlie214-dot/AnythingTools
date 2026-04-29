# utils/logger/__init__.py
"""
================================================================================
DEVELOPER CONTRACT: Dual Logger — The Rule of Detail
================================================================================
Dual logging means every log entry goes to TWO destinations:
1. CONSOLE (stdout) → Notification stream (Brief/Human-readable).
2. DATABASE (logs.db) → Complete structured audit trail (Detailed/JSON).

RULE 1: Console tells you SOMETHING happened.
RULE 2: Database tells you EXACTLY what happened.
RULE 3: payload=None is a CONTRACT VIOLATION. Payload must contain detailed untruncated data.

MANDATORY TAG FORMAT: Category:Sub-Category:Action

PAYLOAD EXAMPLES:
Startup:DB:Probing → {phase: "probing", db_path: "...", status: "EXISTS"}
DB:Validate:Success → {db: "Main", table: "jobs", columns: [...], indexes: [...]}
================================================================================
"""
# Submodules are imported in strict DAG order so each module's dependencies
# are guaranteed to be initialized before the module that needs them.

import utils.logger.setup      # colorama fix; no intra-package deps
import utils.logger.state      # shared ContextVar and mutable state
import utils.logger.routing    # _LOG_DIR
import utils.logger.formatters # formatters and serialization helpers
import utils.logger.handlers   # handler singletons and exc_info normalization
import utils.logger.core       # SumAnalLogger class and public API functions

from utils.logger.core import (
    get_dual_logger,
    global_log_purge,
    flush_all_log_handlers,
)

__all__ = [
    "get_dual_logger",
    "global_log_purge",
    "flush_all_log_handlers",
]
