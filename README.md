# AnythingTools - Unified Agent Framework

## 1. Project Overview

### Concrete, Operational Description
AnythingTools is a **FastAPI-based unified agent framework** that orchestrates autonomous and programmatic AI tool execution through a state-machine architecture. The system operates as a persistent background service that:

- **Maintains job queue** in SQLite database (`jobs` table)
- **Spawns worker threads** via `UnifiedWorkerManager` that poll for queued jobs
- **Executes tools** through `UnifiedAgent` state machines with mode-based personas
- **Persists complete execution history** in `execution_ledger` table (SSSOT)
- **Enforces safety limits** including 50-tool-call hard caps and caller-level locking
- **Supports recovery** by resuming interrupted jobs on restart

### Problem Solved
The system replaces a **tool-centric architecture** where each tool had:
- Monolithic, non-reusable execution logic
- No unified state management
- Manual orchestration required
- No recovery mechanism on crashes
- No shared context between operations

Current architecture provides:
- **Unified agent** with 6 persona modes (Scout, Analyst, Editor, Herald, Quant, Archivist)
- **Execution ledger** as immutable single source of truth
- **Automatic recovery** via startup scans and INTERRUPTED job injection
- **Session continuity** via caller-level locking preventing concurrent execution for same user

### Explicit Non-Goals
- **Does NOT** provide a traditional REST API for direct tool calls (uses job queue pattern)
- **Does NOT** support parallel execution for the same caller_id (locked)
- **Does NOT** maintain conversation history outside the `execution_ledger` table
- **Does NOT** implement actual AI model inference (relies on external LLM clients)
- **Does NOT** provide a user interface (API-only system)

## 2. High-Level Architecture

### Major Components

#### a) API Layer (`api/routes.py`)
- **FastAPI router** with job submission endpoint
- **API key security** via header validation
- **Job lifecycle management**: create, status check, cancel
- **Metrics endpoint** for system health monitoring

#### b) Worker Manager (`bot/engine/worker.py`)
```python
class UnifiedWorkerManager:
    - Polls SQLite `jobs` table every 1.0s
    - Claims jobs with status 'QUEUED' or 'INTERRUPTED' (priority order)
    - Enforces caller-level locking via `_active_callers` set
    - Spawns execution threads with context isolation
    - Injects recovery messages for INTERRUPTED jobs
    - Cleans up locks on job completion/failure
```

#### c) Unified Agent (`bot/core/agent.py`)
```python
class UnifiedAgent:
    - State machine executing Think-Act-Observe loop
    - 50-tool-call hard cap enforcement
    - Mode switching via `system:switch_mode` tool
    - Programmatic vs Autonomous execution types
    - LLM invocation with tool schema injection
```

#### d) Tool Registry (`tools/registry.py`)
```python
class ToolRegistry:
    - Dynamic discovery of BaseTool subclasses
    - Auto-scans `tools/actions/<scope>/` directories
    - Extracts schemas from INPUT_MODEL classes
    - Provides MCP-style manifest via schema_list()
```

#### e) Database Layer
- **Writer Thread** (`database/writer.py`): Single-writer queue for WAL-safe operations
- **Schema** (`database/schema.py`): 11 tables including execution_ledger, jobs, job_items
- **Connection Manager** (`database/connection.py`): Manages read/write connections

### Data Flow

#### Programmatic Execution Flow (Scout Mode)
```
[User/API] → POST /api/tools/{tool_name} → [Router]
    ↓
Creates job record with status 'QUEUED'
    ↓
[Worker Manager] polls job → Spawns thread → [UnifiedAgent]
    ↓
Mode: PROGRAMMATIC → Direct tool execution → [Tool Runner]
    ↓
Tool executes once → Returns result → Writes to execution_ledger
    ↓
Job marked COMPLETED/FAILED → Result returned via GET /api/job/{id}
```

#### Autonomous Execution Flow (Analyst, Editor, etc.)
```
[User/API] → POST /api/tools/research → [Router]
    ↓
Creates job record with status 'QUEUED'
    ↓
[Worker Manager] polls job → Spawns thread → [UnifiedAgent]
    ↓
Mode: AUTONOMOUS → LLM decides next action
    ↓
Loop: (50 call max)
    1. Build context from execution_ledger
    2. Call LLM with tool schemas
    3. Parse tool_calls from response
    4. Execute tools
    5. Record in execution_ledger
    6. Check for mode switch
    7. Continue or return final response
    ↓
Job marked COMPLETED/FAILED
```

### Execution Model
- **Runtime**: Async event loop in FastAPI, threading for worker and DB writer
- **State Persistence**: SQLite with WAL mode
- **Job Queue**: Single-writer pattern prevents DB lock contention
- **Event-Driven**: Worker polls database, agents react to LLM tool_calls
- **Lifecycle**: Jobs progress through PENDING → QUEUED → RUNNING → (INTERRUPTED) → COMPLETED/FAILED

## 3. Repository Structure

```
AnythingTools/
├── app.py                          # FastAPI entrypoint with lifespan hooks
├── config.py                       # Environment configuration with aliases
├── requirements.txt                # Python dependencies
├── snowflake_private_key.p8        # Crypto key (ignored by git)
├── .env                            # Environment variables (git-ignored)
│
├── api/
│   ├── routes.py                   # Job endpoints, API key security
│   ├── schemas.py                  # Pydantic models for requests/responses
│   └── telegram_notifier.py        # Optional push notifications
│
├── bot/
│   ├── core/
│   │   ├── modes.py                # 6 persona mode definitions (SSSOT)
│   │   ├── agent.py                # UnifiedAgent state machine
│   │   └── weaver.py               # Context assembler with budget enforcement
│   ├── engine/
│   │   ├── worker.py               # UnifiedWorkerManager (polls jobs)
│   │   └── tool_runner.py          # Safe tool execution wrapper
│   ├── capabilities/
│   │   └── system_tools.py         # 3 system tools (checklist, mode switch)
│   ├── orchestrator/
│   │   ├── context.py              # Budget-aware context builder
│   │   └── eviction.py             # LRU cache for sessions
│   └── telemetry.py                # Telemetry placeholder
│
├── tools/
│   ├── base.py                     # BaseTool abstract class
│   ├── registry.py                 # Dynamic tool discovery
│   ├── library_query.py            # Legacy public entry point
│   ├── research/                   # Analyst mode initializer
│   │   ├── tool.py                 # Spawns UnifiedAgent(Analyst)
│   │   ├── Skill.py                # Metadata
│   │   └── ...
│   ├── scraper/                    # Scout mode implementation
│   │   ├── tool.py                 # Programmatic web extraction
│   │   ├── browser.py
│   │   └── extraction.py
│   ├── finance/                    # Quant mode initializer
│   ├── publisher/                  # Herald mode initializer
│   ├── draft_editor/               # Editor mode initializer
│   ├── search/
│   ├── vector_memory/
│   ├── quiz/
│   ├── polymarket/
│   ├── logger_agent/
│   └── actions/                    # Agent-action namespaces
│       ├── browser/
│       ├── library/
│       └── system/
│
├── clients/
│   ├── llm/
│   │   ├── factory.py              # LLM provider factory
│   │   ├── providers/
│   │   │   ├── azure.py
│   │   │   └── chutes.py
│   │   └── payloads.py
│   └── snowflake_client.py         # Embedding client
│
├── database/
│   ├── schema.py                   # DB initialization (11 tables)
│   ├── writer.py                   # Background writer (single-writer)
│   ├── connection.py               # Connection manager
│   ├── reader.py
│   └── job_queue.py
│
├── utils/
│   ├── logger/
│   │   ├── core.py                 # Dual logger (console + file)
│   │   ├── routing.py              # Debugger file map
│   │   └── handlers.py
│   ├── browser_daemon.py           # WebDriver lifecycle
│   ├── browser_lock.py             # Singleton browser enforcement
│   ├── budget.py                   # Cost calculations
│   ├── context_helpers.py          # Thread context utilities
│   ├── artifacts.py                # Artifact URL generation
│   ├── security.py                 # URL validation
│   └── ...
│
└── tests/
    └── test_browser_e2e.py         # E2E test for Scout flow
```

### Unconventional Structures

**Directory-named tools**: Tools reside in `tools/research/`, `tools/scraper/`, etc., but the registry extracts class names to produce flat tool names like `research`, `scraper`.

**Multiple system_tools.py**: There's `bot/capabilities/system_tools.py` (3 tools) and `tools/actions/system/` (state, files, skills). The former is used by agent; the latter appears unused based on current imports.

**No tests directory with unit tests**: Only `tests/test_browser_e2e.py` exists, suggesting most testing is ad-hoc or not yet implemented.

**Worker/manager.py deleted**: Recent commit removed legacy worker manager. Current implementation is `bot/engine/worker.py`. Git shows no `worker/manager.py` exists in current tree.

## 4. Core Concepts & Domain Model

### Key Abstractions

**AgentMode** (`bot/core/modes.py`)
```python
@dataclass
class AgentMode:
    name: str                    # "Analyst", "Scout", etc.
    execution_type: str          # "PROGRAMMATIC" or "AUTONOMOUS"
    system_prompt: str           # Persona instruction
    allowed_tools: List[str]     # Tool namespace permissions
```

**UnifiedAgent** (`bot/core/agent.py`)
- **Job-scoped**: Initialized per job with `job_id`, `caller_id`, `initial_mode`
- **Stateful**: Tracks `tool_call_count`, `current_mode`
- **LLM-powered**: Uses Azure LLM client by default
- **50-call cap**: Hard limit prevents infinite loops

**BaseTool** (`tools/base.py`)
```python
class BaseTool:
    name: str
    async def run(self, args: dict, telemetry, **kwargs) -> str
    def is_resumable(self, args: dict) -> bool  # marker for recovery
```

**ToolRegistry** (`tools/registry.py`)
- **Lazy loading**: Tools discovered on `load_all()` call
- **Multi-scope**: Handles legacy top-level and `actions/` namespaces
- **Schema extraction**: Reads `INPUT_MODEL` from tool modules
- **Dynamic instantiation**: `create_tool_instance(name)` creates fresh tool per job

### Data Models

**execution_ledger** - Single Source of Truth
```sql
CREATE TABLE execution_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_id TEXT UNIQUE NOT NULL,
    job_id TEXT NOT NULL,
    caller_id TEXT,              -- stringified chat_id
    role TEXT CHECK(role IN ('system','user','assistant','tool')),
    content TEXT NOT NULL,
    char_count INTEGER NOT NULL, -- Used for budget enforcement
    attachment_metadata TEXT,    -- JSON of file paths
    timestamp TEXT NOT NULL
)
```

**jobs** - Work Queue
```sql
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL,
    status TEXT CHECK(status IN (
        'PENDING','QUEUED','RUNNING','INTERRUPTED',
        'COMPLETED','FAILED','ABANDONED','CANCELLING'
    )),
    result_json TEXT,            -- Final payload
    created_at TEXT,
    updated_at TEXT
)
```

### Implicit Rules & Assumptions

1. **Caller ID is chat_id as string**: `str(chat_id)` used throughout, but `jobs.chat_id` is INTEGER
2. **Tool names are unique**: Registry returns first match; no namespace collision handling
3. **Single browser instance**: `browser_lock.py` enforces singleton browser
4. **WAL mode required**: Writer thread assumes SQLite WAL for concurrent access
5. **env vars are source of truth**: config.py reads all values from `os.getenv()` with defaults
6. **Job items are ephemeral**: `job_items` table tracks steps but is not critical path
7. **Recovery uses INTERRUPTED status**: Only jobs with this status get recovery message injection
8. **Programmatic tools return early**: `PROGRAMMATIC` mode returns immediately after single execution

## 5. Detailed Behavior

### Normal Execution - Autonomous (Analyst)
1. **API receives** `POST /api/tools/research` with `{url, goal}`
2. **Router creates** job record in `jobs` table with status `QUEUED`
3. **Background writer** persists job to database
4. **WorkerManager** poll loop (every 1s) detects new job
5. **Caller lock check**: `caller_id` added to `_active_callers`; existing callers skip
6. **Thread spawns** with context isolation: `spawn_thread_with_context(_run_job, ...)`
7. **Job marked** `RUNNING` via `enqueue_write()`
8. **Agent instantiated**: `UnifiedAgent(job_id, caller_id, "Analyst")`
9. **Mode check**: `execution_type == "AUTONOMOUS"` → enters loop
10. **Context build**: `build_session_context()` queries `execution_ledger` for this caller
11. **LLM call**: Agent sends system prompt + context + tool schemas to Azure
12. **LLM response**: Returns either:
    - **No tool calls**: Final assistant message → record in ledger → return COMPLETED
    - **Tool calls**: List of function invocations
13. **Tool execution**: For each call:
    - Increment `tool_call_count` (enforces 50 cap)
    - Record intent in ledger (role: "assistant")
    - Check for `system:switch_mode` → update `current_mode` if valid
    - Create tool instance via `REGISTRY.create_tool_instance()`
    - Run via `run_tool_safely()` which wraps exceptions
    - Record tool response in ledger (role: "tool")
14. **Loop continues** or returns based on LLM behavior
15. **Job completion**: Status set to `COMPLETED` or `FAILED`, result_json populated
16. **Lock release**: `caller_id` removed from `_active_callers`

### Edge Cases & Failure Modes

**Infinite Loop Prevention**
- **Problem**: LLM calls same tool repeatedly
- **Solution**: Hard cap at 50 tool calls → returns `{status: FAILED, message: "Hard limit exceeded"}`

**Caller Contention**
- **Problem**: Same caller submits multiple jobs simultaneously
- **Solution**: Caller-level locking; only one job per `caller_id` active at a time

**Database Writer Failure**
- **Problem**: Writer thread crashes or queue overflows
- **Solution**: `enqueue_write()` falls back to starting new writer thread; logs warning on queue full

**Startup Crash Recovery**
- **Problem**: Jobs marked `RUNNING` when process died
- **Solution**: `app.py` lifespan scans `jobs WHERE status = 'RUNNING'` → marks `INTERRUPTED`
- **Next startup**: Worker sees `INTERRUPTED` → injects recovery message into ledger

**LLM Returns Invalid JSON**
- **Problem**: Tool call arguments fail to parse
- **Solution**: `run_tool_safely()` catches `json.JSONDecodeError` → returns error result

**PaddleOCR v3.x Compatibility**
- **Problem**: Old args (`use_angle_cls`, `lang`, `show_log`) cause unknown argument errors
- **Solution**: `prefetch_paddleocr()` uses v3.x args (`use_doc_orientation_classify`, etc.)

**UnboundLocalError in Writer**
- **Problem**: `_write_generation` referenced before assignment in exception paths
- **Solution**: Added `global _write_generation` declaration at function start

### Configuration Paths

**Environment Variable Loading**
- `config.py` calls `load_dotenv()` immediately (line 6)
- All config values read from `os.getenv()` at module import time
- Alias mappings like `AZURE_KEY = os.getenv("AZURE_KEY") or AZURE_OPENAI_KEY`

**Behavior Changes via Config**
- `TELEMETRY_DRY_RUN`: Controls whether telemetry is actually sent
- `JOB_WATCH_INTERVAL_SECONDS`: Polling frequency for worker (default 300s)
- `LOGGER_TRUNCATION_LIMIT`: Max bytes per log entry (default 5MB)
- `MODEL_MAX_CONTEXT_CHARS`: LLM context budget for weaver

## 6. Public Interfaces

### CLI / Entry Points

**FastAPI Endpoints** (via `app.py`)
```python
# Job submission
POST /api/tools/{tool_name}
Body: {
  "args": {...},           # Tool-specific arguments
  "client_metadata": {...} # Optional metadata
}
Header: X-API-Key: <config.API_KEY>
Returns: {"job_id": "...", "status": "QUEUED"}

# Status check
GET /api/tools/{job_id}
Header: X-API-Key: <config.API_KEY>
Returns: {
  "status": "...",
  "job_logs": [...],       # From execution_ledger
  "final_payload": {...}   # From jobs.result_json
}

# Job cancellation
DELETE /api/tools/{job_id}
Header: X-API-Key: <config.API_KEY>
Returns: {"status": "CANCELLING"}

# Metrics
GET /metrics
Header: X-API-Key: <config.API_KEY>
Returns: {
  "write_queue_size": 0,
  "active_jobs": 0,
  "registered_tools": 42
}
```

### Function/Class APIs

**Tool Discovery**
```python
from tools.registry import REGISTRY

REGISTRY.load_all()                    # Call before using registry
all_tools = REGISTRY.schema_list()     # Get MCP manifest
tool_cls = REGISTRY.get_tool_class("research")
instance = REGISTRY.create_tool_instance("scraper")
```

**Agent Execution**
```python
from bot.core.agent import UnifiedAgent

agent = UnifiedAgent(job_id="job_123", caller_id="user_456", initial_mode="Analyst")
result = await agent.run(telemetry_callback, url="https://...", goal="analyze")
# Returns: {"status": "COMPLETED", "result": "..."} or {"status": "FAILED", "message": "..."}
```

**Database Writes**
```python
from database.writer import enqueue_write, enqueue_execscript

enqueue_write("INSERT INTO jobs VALUES (?, ?, ?)", (job_id, tool_name, args_json))
enqueue_execscript("CREATE TABLE ...; INSERT INTO ...;")
```

### Expected Inputs/Outputs

**Input Constraints**
- **Tool args**: Must match input_schema from registry (or free-form if none)
- **Job IDs**: ULID format (26-char, lexicographically sortable)
- **Chat IDs**: Integer but stored as string in caller_id fields
- **Content size**: No hard limit, but exceeding budget triggers "Guillotine" truncation

**Output Format**
- **Programmatic tools**: Direct string output
- **Autonomous agents**: Final assistant message or structured result
- **Errors**: LLM-diagnosed error messages in `ToolResult.output`

**Side Effects**
- **Database writes**: Asynchronous via queue
- **Ledger entries**: Immutable history recording every step
- **File artifacts**: Stored in `artifacts/` directory, path in `attachment_metadata`

## 7. State, Persistence, and Data

### Storage Locations

**Primary Database**: `database/` folder (SQLite)
- `database.db`: Main database file
- `database.db-wal`: Write-ahead log
- `database.db-shm`: Shared memory file

**Artifact Storage**: `artifacts/` directory
- User-generated files (PDFs, images, scraped content)
- Referenced in `execution_ledger.attachment_metadata` as JSON

**Logs**: `logs/` directory
- Financial formula logs
- Debug logs via logger agent

### Data Formats

**execution_ledger.content**: Raw text or JSON string
**execution_ledger.attachment_metadata**: 
```json
{
  "screenshot": "artifacts/job_123/screenshot.png",
  "pdf": "artifacts/job_123/report.pdf"
}
```

**jobs.result_json**:
```json
{
  "status": "COMPLETED",
  "result": "Final message or structured data",
  "artifacts": [{"id": "...", "relpath": "..."}],
  "error_details": null,
  "metrics": {"duration_seconds": 15.3}
}
```

### Lifecycle

**Job Lifecycle**
1. **Creation**: `QUEUED` status, `created_at` timestamp
2. **Claim**: `RUNNING` status, `updated_at` timestamp
3. **Recovery**: `INTERRUPTED` status (if crash detected)
4. **Completion**: `COMPLETED`/`FAILED` status, `result_json` populated

**Session Cleanup**
- **Stale sessions**: `purge_stale_sessions(7)` deletes data older than 7 days
- **PDF cache**: Cleared on startup and shutdown (`DELETE FROM pdf_parsed_pages`)
- **Job items**: Deleted when job is purged

**Embedded Content**
- `scraped_articles` table stores embeddings in `scraped_articles_vec`
- `long_term_memories` table stores embeddings in `long_term_memories_vec`
- Uses `sqlite_vec` extension (with fallback to BLOB tables if unavailable)

### Migration Behavior

**Schema Versioning**: `SCHEMA_VERSION = 1`
**Reset Logic**: 
- If current version < target version AND `SUMANAL_ALLOW_SCHEMA_RESET=1`
- Performs destructive reset backed up by `ltm_backup` table
- Restores long-term memories after reset

**Legacy Table Migration**: 
- `chat_messages` → `execution_ledger` (complete replacement)
- `sessions` → `chats` + `jobs` (refactored structure)

## 8. Dependencies & Integration

### External Libraries (from requirements.txt)

**FastAPI Ecosystem**
- `fastapi>=0.100.0`: Web framework
- `uvicorn[standard]>=0.22.0`: ASGI server
- `pydantic>=1.10.7`: Data validation

**Browser Automation**
- `botasaurus>=1.0.0`: Headless browser management
- `puppeteer` (implied by browser tools, not explicit in requirements)

**LLM/Providers**
- `openai>=1.0.0`: OpenAI API client
- `snowflake-connector-python>=3.0.0`: For embeddings (optional)

**PDF/OCR**
- `paddlepaddle==3.2.0`: CPU-specific ML framework
- `paddleocr`: OCR engine (v3.x compatible)
- `pymupdf`: PDF manipulation
- `pypdf`, `pdfplumber`: PDF parsing

**Data Science**
- `pandas>=2.0.0`: Data manipulation
- `numpy` (implicit from pandas)
- `yfinance>=0.2.18`: Financial data
- `edgartools>=2.0.0`: SEC filings
- `sec-edgar-downloader>=4.0.0`

**Utilities**
- `python-dotenv>=1.1.0`: Environment loading
- `colorama>=0.4.6`: Colored logging
- `psutil==5.9.5`: Process management
- `sqlite-vec>=0.1.0`: Vector search in SQLite

### Why Each Dependency Exists (Code Evidence)

- **snowflake-connector-python**: `clients/snowflake_client.py` imports; used for embeddings
- **cryptography**: Required for Snowflake private key authentication (Python 3.10+ requirement)
- **paddlepaddle**: `app.py` line 27 imports `PaddleOCR`; runtime dependency
- **botasaurus**: `utils/browser_daemon.py` uses for WebDriver lifecycle
- **python-dotenv**: Now called in `config.py` line 4-6 (after PLAN-01 fix)

### Coupling Points

**Hard Dependencies**
- `app.py` → `config.py` (imports and uses at module level)
- `bot/engine/worker.py` → `config` (imports and reads `JOB_WATCH_INTERVAL_SECONDS`)
- `database/writer.py` → `config` (imports and reads)

**Soft Dependencies** (optional features)
- **Telegram**: `api/telegram_notifier.py` only imported if notification needed
- **Snowflake**: Only imported if embeddings requested
- **vec0 extension**: Checked at startup; falls back to BLOB tables

### Environment Assumptions

- **Python 3.10+** (union types: `str | None`)
- **Windows/Linux/macOS** (SQLite is cross-platform)
- **Network access**: Required for LLM, browser, financial APIs
- **Disk space**: For artifacts, database, PDF cache
- **Memory**: LLM context windows, browser instance

## 9. Setup, Build, and Execution

### Clean Environment Setup

```bash
# 1. Clone repository
git clone <repo>
cd AnythingTools

# 2. Create virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install sqlite_vec extension (optional, requires binary)
# Download from: https://github.com/asg017/sqlite-vec/releases
# Place binary in working directory or system PATH

# 5. Configure environment
cp .env.example .env  # If example exists
# Edit .env with required values:
# - API_KEY (any string for dev)
# - AZURE_OPENAI_KEY, AZURE_OPENAI_ENDPOINT (for LLM)
# - Other optional keys as needed

# 6. Verify PaddlePaddle installation
python -c "from paddleocr import PaddleOCR; PaddleOCR()"
# Should download models on first run
```

### Running the System

**Development Mode**
```bash
# Start FastAPI server (hot reload enabled)
uvicorn app:app --reload --port 8000

# Server will:
# 1. Load environment variables
# 2. Initialize database schema
# 3. Start background writer thread
# 4. Validate sqlite_vec extension
# 5. Warm up browser (Scout mode)
# 6. Reconcile pending embeddings
# 7. Scan for interrupted jobs
# 8. Purge stale sessions (>7 days)
# 9. Load tool registry
# 10. Start PaddleOCR prefetch
# 11. Ready to accept API requests
```

**Production Mode**
```bash
# With process manager (systemd example)
# /etc/systemd/system/anythingtools.service
[Unit]
Description=AnythingTools Unified Agent
After=network.target

[Service]
Type=simple
User=anythingtools
WorkingDirectory=/opt/anythingtools
Environment=SUMANAL_ALLOW_SCHEMA_RESET=0
ExecStart=/opt/anythingtools/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

### Platform Constraints

**SQLite Version**: Must support WAL mode (3.7.0+)
**Browser**: Chrome/Chromium must be installed for Scout mode
**PaddlePaddle**: CPU-only installation via `--extra-index-url` in requirements
**Memory**: Minimum 2GB RAM, 4GB+ recommended for autonomous modes

### Build Processes

**No compilation required**: Pure Python
**No asset bundling**: Tools loaded dynamically
**Database**: Auto-initialized on first run via `get_init_script()`

## 10. Testing & Validation

### Existing Tests

**E2E Test** (`tests/test_browser_e2e.py`)
```python
def test_wikipedia_summary():
    # Verifies Scout → Agent → Ledger flow
    # Confirms char_count tracking
    # Validates ledger persistence
```

**Coverage**: Only tests Scout mode end-to-end; no unit tests for other components.

### How to Run Tests

```bash
# Run E2E test
pytest tests/test_browser_e2e.py -v

# No test discovery configured; run manually
# No test fixtures or mocks exist
```

### Test Gaps (Visible from Code)

1. **No unit tests** for `UnifiedAgent` state machine
2. **No tests** for caller-level locking behavior
3. **No tests** for recovery injection logic
4. **No tests** for ToolRegistry dynamic loading
5. **No tests** for budget enforcement (Guillotine)
6. **No tests** for mode switching via `system:switch_mode`
7. **No tests** for database writer contention
8. **No tests** for error diagnosis in `run_tool_safely()`

### Manual Validation Commands

```bash
# Check registry
python -c "from tools.registry import REGISTRY; REGISTRY.load_all(); print(len(REGISTRY.schema_list()))"

# Verify database
sqlite3 database.db "SELECT COUNT(*) FROM execution_ledger;"

# Test config loading
python -c "import config; print(config.AZURE_KEY is not None)"
```

## 11. Known Limitations & Non-Goals

### Hard Constraints

**50 Tool Call Hard Cap**
- Enforced in `bot/core/agent.py` line 34
- Cannot be disabled via configuration
- Exceeding returns failure status

**Single Caller Lock**
- `bot/engine/worker.py` line 86-87
- Only 1 job per `caller_id` at a time
- No queueing; subsequent jobs skip until first completes

**Programmatic Mode Early Exit**
- `bot/core/agent.py` line 36-42
- Executes tool directly, returns immediately
- No LLM involvement or loop

**Character Budget Enforcement**
- Context trimming in `bot/core/weaver.py`
- Guillotine truncation if total chars > budget
- No warning before truncation

**No Persistent Conversation**
- `execution_ledger` contains only current job history
- No cross-job context sharing
- Caller ID used as isolation key

### Features That Appear Implied But Don't Exist

**Parallel Tool Execution**
- No thread pool for concurrent tool calls
- Tools executed sequentially in agent loop

**Automatic Retry**
- `retry_count` field exists in jobs table but no retry logic in code

**Scheduled Jobs**
- No cron or scheduler mechanism
- All jobs triggered by API calls

**Input Validation**
- Most tools accept any args; only `research` validates URL
- Types not enforced beyond JSON parsing

**Security Hardening**
- API key is single string, no RBAC
- No rate limiting on endpoints
- No input sanitization beyond URL validation

### Trade-offs

**Speed vs Safety**
- Single-writer DB pattern prevents lock contention but adds latency
- Caller locking prevents concurrency but ensures session continuity

**Flexibility vs Type Safety**
- Dynamic tool loading allows new tools without restart
- No compile-time validation of tool signatures

**Recoverability vs Simplicity**
- Recovery scan adds startup time
- Complex resume logic for INTERRUPTED jobs

## 12. Change Sensitivity

### Most Fragile Components

**1. Tool Registry (`tools/registry.py`)**
- **Why**: Dynamic import of arbitrary modules
- **Risk**: Import errors crash registry load
- **Change Impact**: High - affects all tool discovery
- **Extension Easiest**: Add new module under `tools/actions/<scope>/`

**2. Database Schema (`database/schema.py`)**
- **Why**: Direct SQLite operations, no ORM
- **Risk**: Schema changes require migration scripts
- **Change Impact**: Critical - breaks existing data
- **Extension Hardest**: Requires migration plan

**3. Worker Manager (`bot/engine/worker.py`)**
- **Why**: Thread management, locking, polling loop
- **Risk**: Deadlocks, race conditions on `_active_callers`
- **Change Impact**: High - affects job execution reliability
- **Fragile Areas**: Lock cleanup in `finally` block

**4. Unified Agent (`bot/core/agent.py`)**
- **Why**: Complex state machine, LLM integration
- **Risk**: Infinite loops, context budget bugs
- **Change Impact**: High - core execution logic
- **Fragile Areas**: 50-call cap, mode switching

**5. Config Module (`config.py`)**
- **Why**: Module-level `load_dotenv()` and variable assignment
- **Risk**: Import time side effects, ordering issues
- **Change Impact**: Medium - affects all components
- **Fragile Areas**: Alias mappings (e.g., `AZURE_KEY` vs `AZURE_OPENAI_KEY`)

### Easiest to Extend

**Tool Addition**: Add new directory under `tools/actions/<scope>/` with `tool.py` and `Skill.py`
**Mode Addition**: Add entry to `MODES` dict in `bot/core/modes.py`
**LLM Provider**: Add class to `clients/llm/providers/`, update factory

### Hardest to Modify

**Database Schema**: Requires understanding of execution_ledger dependencies
**Worker Logic**: Threading issues are hard to debug; affects entire system
**Agent Loop**: Change could break 50-call cap or recovery behavior