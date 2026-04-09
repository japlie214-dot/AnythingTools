# utils/logger/__init__.py
"""
================================================================================
DEVELOPER CONTRACT: Tag Naming Convention - The Rule of Three
================================================================================

MANDATORY TAG FORMAT: Category:Sub-Category:Action

This "Rule of Three" is the Developer Contract that ensures the Debugger Agent
can automatically route to the correct code "neighborhood" for any future feature.

THE THREE REQUIRED PARTS:

1. CATEGORY: The broad architectural layer
   - API: FastAPI endpoints, request handling
   - Worker: Background job processing, thread management
   - DB: Database operations, connections, transactions
   - Vision: Image capture, processing, analysis
   - PDF: Document extraction, parsing
   - Tool: Tool execution, registry operations
   - LLM: Language model clients, prompts
   - Scraper: Web scraping, browser automation
   - Research: Multi-step research workflows
   - Sys: Application lifecycle (seeded, not used by logic)

2. SUB-CATEGORY: The specific module or service
   - Job: Individual work units, queue management
   - Manager: Worker orchestration, lifecycle control
   - Writer: Database write operations, WAL
   - Reader: Database read operations, queries
   - Extract: Data extraction from documents
   - Capture: Screenshot/image acquisition
   - Navigate: Browser page navigation
   - Create: Resource instantiation
   - Persist: Data storage operations
   - Start/Stop: Lifecycle transitions

3. ACTION: The specific operation being performed
   - Create/Destroy: Resource lifecycle
   - Start/Stop: Process control
   - Prepare/Execute: Workflow stages
   - Commit/Rollback: Transaction states
   - Slicing/Tiling: Image processing steps
   - Parse/Validate: Data verification
   - Poll/Watch: Monitoring operations
   - Load/Unload: Resource management

================================================================================
WHY THE RULE OF THREE MATTERS
================================================================================

Example Scenario: A developer adds "VideoProcessing" next week.

GOOD (Automatic Neighborhood Routing):
  Video:Capture:Frame       → Routes to video capture code
  Video:Encode:Compress     → Routes to video encoding code
  Video:Store:Chunk         → Routes to video storage code

BAD (Ambiguous, Debugger Confused):
  Video:Error               → Which part failed?
  VideoProcessing           → What operation?

The Debugger Agent uses prefix matching on the first two parts to select
which files to include in the diagnostic context. The third part helps
distinguish between different failure modes in the same module.

================================================================================
RESERVED PREFIXES - NEVER USE IN APPLICATION CODE
================================================================================

Debugger: — EXCLUSIVE TO DIAGNOSTIC AGENT
  - Never emit logs with tag="Debugger:..."
  - Using this causes infinite loop: Debugger triggers → emits Debugger tag → triggers again
  - The Agent uses this prefix internally for its own diagnostic reports

Sys: — APPLICATION LIFECYCLE ONLY
  - Reserved for: Sys:Startup:*, Sys:Shutdown:*, Sys:Config:*
  - Logic code should emit specific categories (not use Sys)
  - Example: Use API:Writer:Start instead of Sys:Startup:Writer

================================================================================
LOGGING SEVERITY DEFINITIONS (LOCKED)
================================================================================

- DEBUG: Low-level tracing and granular variable states. Ignored by Debugger Agent.
- INFO: Standard operational milestones and state changes. Ignored by Debugger Agent.
- WARNING: Recoverable anomalies, failed retries, or degraded modes. Triggers Debugger Agent.
- ERROR: Operation failures preventing a single feature/task from succeeding. Triggers Debugger Agent.
- CRITICAL: System-wide failures requiring immediate intervention. Triggers Debugger Agent.

FAILURE SCENARIO EXAMPLES:

Vision:Capture:Screenshot → Camera unavailable
PDF:Extract:Parse         → Password-protected document
Worker:Job:Crashed        → Tool execution exception
DB:Writer:Commit          → Constraint violation

Each tag pattern maps to a specific "neighborhood" of files in the routing table.
================================================================================
"""
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
