# utils/logger/routing.py
from pathlib import Path

# Shared by handlers.py (handler file creation) and core.py (clear_sql_log,
# global_log_purge). Defined here — earlier in the DAG than both consumers —
# to provide a single source of truth without cross-consumer coupling.
_LOG_DIR = Path("logs")

LOG_MAP: dict[str, str] = {
    "Income Statement":   "log_Incomeformula.txt",
    "Balance Sheet":      "log_Balanceformula.txt",
    "Cash Flow":          "log_Cashformula.txt",
    "Quarterly Earnings": "log_PEformula.txt",
    "Shares Outstanding": "log_PEformula.txt",
}

# Static domain-to-file routing map. Keys are tag prefixes; values are ordered
# file lists scanned until the context budget is exhausted. "DEFAULT" is a
# sentinel — never matched by startswith(); insertion order is scan priority.
# NOTE: "utils/logger_util.py" has been replaced with "utils/logger/core.py"
# and "utils/logger/state.py".
# !! MANDATORY MAINTENANCE NOTICE !!
# Static domain-to-file routing map used by the Debugger Agent.
# 1. SPECIFICITY RULE: Define specific, longer prefixes (e.g., 'Browser:Warmup')
#    BEFORE their broader fallback prefixes (e.g., 'Browser:'). The first match
#    triggers a break; misplaced broad prefixes will overshadow specific ones.
# 2. PRIORITY RULE: Order file lists by diagnostic criticality. The agent scans
#    files until the context budget is exhausted; primary modules must be at index 0.
# 3. SENTINEL RULE: The 'DEFAULT' key must always remain the final entry.
DEBUGGER_FILE_MAP: dict[str, list[str]] = {
    "DB:Writer": ["database/writer.py", "database/connection.py", "config.py"],
    "DB:Reader": ["database/reader.py", "database/connection.py"],
    "API:Job": ["api/routes.py", "database/job_queue.py", "api/schemas.py", "utils/id_generator.py"],
    "Worker:Manager": ["worker/manager.py", "database/connection.py", "database/job_queue.py", "config.py"],
    "Worker:Job": ["worker/manager.py", "tools/registry.py", "utils/context_helpers.py", "tools/base.py"],
    "Vision:Capture": ["utils/vision_utils.py", "utils/browser_daemon.py", "utils/browser_lock.py"],
    "PDF:Extract": ["utils/pdf_utils.py", "utils/budget.py"],
    "LLM:": ["clients/llm/factory.py", "clients/llm/payloads.py", "clients/llm/types.py"],
    "Scraper:Browser:Navigate": ["tools/scraper/extraction.py", "utils/browser_utils.py", "utils/browser_daemon.py", "utils/browser_lock.py", "config.py"],
    "Scraper:Extract:HTML": ["tools/scraper/browser.py", "utils/text_processing.py", "tools/scraper/targets.py", "utils/browser_utils.py"],
    "Scraper:Extract": ["tools/scraper/extraction.py", "tools/scraper/browser.py", "utils/browser_utils.py"],
    "Research:Step": ["tools/research/tool.py", "tools/research/research_prompts.py", "database/job_queue.py"],
    "Sys:Startup": ["app.py", "config.py", "database/schema.py"],
    "DEFAULT": ["app.py", "worker/manager.py", "utils/logger/core.py", "config.py"],
}
