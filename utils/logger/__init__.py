# utils/logger/__init__.py
"""
================================================================================
DEVELOPER CONTRACT: THE RULE OF TOTAL RECONSTRUCTION
================================================================================
The primary goal of logging in AnythingTools is RECONSTRUCTABILITY.
A developer must be able to reconstruct the entire state, input, and output
of a failed job using ONLY the logs.db, without ever looking at the source code.

COMMANDMENT 1: LOG EVERY GRANULAR ACTION
Do not just log 'Tool Started' and 'Tool Finished'. Log every internal decision,
every loop iteration, and every state change. If the code "branches" (if/else),
both paths must be logged.

COMMANDMENT 2: THE I/O SYMMETRY RULE (Audit Boundaries)
Every time data crosses a boundary (LLM call, Database write, Filesystem read,
Snowflake embedding), you MUST log two entries:
    1. THE REQUEST (Input): Full prompt, parameters, or SQL.
    2. THE RESPONSE (Output): Full raw content, latency, and row counts.

COMMANDMENT 3: PAYLOADS ARE DATA, NOT STATUS
Logging payload={"status": "success"} is a CONTRACT VIOLATION.
Payloads must contain the actual content being processed.
    - BAD:  payload={"result": "success"}
    - GOOD: payload={"input_text": "...", "output_vector": [...], "latency_ms": 450}

COMMANDMENT 4: MANDATORY TAG FORMAT (Category:Sub-Category:Action)
Tags must follow a 3-part hierarchy for SQL-based filtering.
    - Valid:   LLM:Azure:Request, DB:Writer:Commit, Scraper:Curation:Selected
    - Invalid: Scraper:Curation (2 parts), Process_Started (No parts)

================================================================================
I/O LOGGING EXAMPLES (MANDATORY PATTERNS)
================================================================================

1. LLM Boundary:
   log.dual_log(tag="LLM:Azure:Request", ..., payload={"prompt": prompt, "temp": 0.3})
   log.dual_log(tag="LLM:Azure:Response", ..., payload={"content": raw_resp, "usage": usage})

2. Embedding Boundary:
   log.dual_log(tag="Embed:Snowflake:Request", ..., payload={"text_len": 1200, "preview": text[:500]})
   log.dual_log(tag="Embed:Snowflake:Response", ..., payload={"dims": 1024, "vector": Base64Vector(v)})

3. Database Boundary:
   log.dual_log(tag="DB:Writer:Insert", ..., payload={"table": "jobs", "rows": 1, "data": row_dict})

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
