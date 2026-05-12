"""utils/logger/tags.py
Central tag registry for AnythingTools logging.
All tags follow the Category:SubCategory:Action convention.
New code MUST reference these constants instead of inline strings.
See utils/logger/__init__.py COMMANDMENT 4 for the contract.
"""
# ── Scraper ────────────────────────────────────────────────
SCRAPER_DEDUP_CHECK = "Scraper:Dedup:Check"
SCRAPER_DEDUP_RESULT = "Scraper:Dedup:Result"
SCRAPER_LINKS_DISCOVER = "Scraper:Links:Discover"
SCRAPER_PROCESS_EXECUTE = "Scraper:Process:Execute"
SCRAPER_PARTIAL_SAVE = "Scraper:Partial:Save"
SCRAPER_CURATION_EXEC = "Scraper:Curation:Execute"
SCRAPER_SELECTOR_WAIT = "Scraper:Selector:WaitStart"
SCRAPER_SCREENSHOT_CAP = "Scraper:Screenshot:Capture"

# ── Vision ────────────────────────────────────────────────
VISION_OPTIMIZE_RESIZE = "Vision:Optimize:Resize"
VISION_CAPTURE_SCREEN = "Vision:Capture:Screenshot"
VISION_GUARD_CHECK = "Vision:Guard:Check"

# ── Text Processing ────────────────────────────────────────
TEXT_PARSE_EXECUTE = "Text:Parse:Execute"
TEXT_CLEANHTML_EXECUTE = "Text:CleanHTML:Execute"

# ── Vector Search ──────────────────────────────────────────
SEARCH_VECTOR_QUERY = "Search:Vector:Query"
SEARCH_CLIENT_RETRY = "Search:Client:Retry"

# ── Debugger ───────────────────────────────────────────────
DEBUGGER_CONTEXT_ASSEMBLY = "Debugger:Context:Assembly"
DEBUGGER_CONTEXT_HALVING = "Debugger:Context:Halving"
DEBUGGER_AGENT_ERROR = "Debugger:Agent:Error"
DEBUGGER_AGENT_REPORT = "Debugger:Agent:Report"

# ── HITL ───────────────────────────────────────────────────
HITL_CANCEL_REQUEST = "HITL:Cancel:Request"

# ── Publisher ──────────────────────────────────────────────
PUBLISHER_INVENTORY_CHECK = "Publisher:Inventory:Check"

# ── Worker ─────────────────────────────────────────────────
WORKER_LOG_EXPORT_WRITE = "Worker:LogExport:Write"

# ── Snowflake ──────────────────────────────────────────────
SNOWFLAKE_CLIENT_INIT = "Snowflake:Client:Init"
SNOWFLAKE_EMBED_RAW = "Embed:Snowflake:Raw"

# ── Telegram ───────────────────────────────────────────────
TELEGRAM_RATELIMIT_THROTTLE = "Telegram:RateLimiter:Throttle"

# ── DB Normalized (ALL_CAPS -> PascalCase) ──────────────────
SYS_BLACKBOARD_INIT = "Sys:Blackboard:Init"
SYS_BLACKBOARD_CLAIM = "Sys:Blackboard:Claim"
SYS_BLACKBOARD_FAILURE = "Sys:Blackboard:Failure"
DB_WRITE_START = "Db:Write:Start"
DB_WRITE_END = "Db:Write:End"
