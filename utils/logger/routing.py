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
    "LLM:":        ["clients/llm_client.py", "config.py"],
    "Finance:":    ["tools/finance/tool.py", "tools/finance/pipeline.py",
                    "tools/finance/ingestion.py", "tools/finance/finance_prompts.py",
                    "config.py"],
    "DB:Reconcile": ["main.py", "clients/snowflake_client.py", "database/writer.py", "database/connection.py"],
    "DB:Recovery": ["database/job_queue.py", "main.py", "database/writer.py", "database/connection.py"],
    "DB:Cleanup":  ["database/job_queue.py", "main.py", "database/writer.py", "database/connection.py"],
    "DB:":         ["database/writer.py", "database/connection.py",
                    "database/schema.py", "database/job_queue.py"],
    "Scraper:":    ["tools/scraper/tool.py", "tools/scraper/task.py",
                    "tools/scraper/extraction.py", "tools/scraper/persistence.py"],
    "Browser:Warmup": ["utils/browser_daemon.py", "main.py", "utils/browser_utils.py", "utils/browser_lock.py", "utils/logger/core.py", "config.py"],
    "Browser:Cleanup": ["utils/browser_daemon.py", "main.py", "config.py"],
    "Browser:Shutdown": ["utils/browser_daemon.py", "main.py", "config.py"],
    "Browser:":    ["utils/browser_daemon.py", "utils/browser_lock.py",
                    "utils/browser_utils.py", "tools/browser/tool.py",
                    "utils/som_utils.py"],
    "Macro:":      ["tools/macros/tool.py", "utils/prompt_cache.py"],
    "Publisher:":  ["tools/publisher/tool.py", "config.py"],
    "Research:":   ["tools/research/tool.py", "tools/research/curator.py",
                    "tools/research/pdf_engine.py", "tools/research/research_prompts.py"],
    "PDF:":        ["utils/pdf_utils.py", "tools/pdf_search/tool.py",
                    "tools/pdf_search/toc_tool.py"],
    "Snowflake:":  ["clients/snowflake_client.py", "utils/vector_search.py"],
    "Sys:":        ["main.py", "config.py", "database/schema.py"],
    "Agent:":      ["bot/orchestrator.py", "bot/handlers.py",
                    "tools/logger_agent/logger_prompts.py", "tools/base.py"],
    "Archivist:":  ["bot/archivist.py"],
    "Skills:":     ["tools/skills/tool.py"],
    "Files:":      ["tools/files/tool.py"],
    "Search:":     ["tools/search/tool.py"],
    "Quiz:":       ["tools/quiz/tool.py", "tools/quiz/quiz_prompts.py"],
    "Vision:":     ["utils/vision_utils.py", "utils/multimodal.py", "tools/research/scraper_agent.py", "config.py"],
    "VectorSearch:": ["utils/vector_search.py", "clients/snowflake_client.py", "database/writer.py", "database/connection.py"],
    "Text:":       ["utils/text_processing.py"],
    "SoM:":        ["utils/som_utils.py", "utils/browser_daemon.py"],
    "IBKR:":       ["tools/ibkr.py", "utils/browser_daemon.py", "utils/browser_lock.py", "config.py"],
    "Polymarket:": ["tools/polymarket/tool.py", "tools/polymarket/polymarket_prompts.py", "clients/llm_client.py", "config.py"],
    "Tool:":       ["tools/base.py", "utils/source_context.py", "config.py"],
    "Bot:Handler:": ["bot/handlers.py", "bot/orchestrator.py", "utils/multimodal.py", "database/writer.py", "config.py"],
    "Callbacks:":  ["bot/callbacks.py", "bot/orchestrator.py", "bot/telemetry.py", "bot/handlers.py"],
    "Telemetry:":  ["bot/telemetry.py", "database/writer.py", "config.py"],
    "Telegram:":   ["bot/telemetry.py", "tools/publisher/tool.py", "bot/callbacks.py", "config.py"],
    "DEFAULT":     ["main.py", "config.py", "utils/logger/core.py",
                    "utils/logger/state.py", "bot/orchestrator.py"],
}
