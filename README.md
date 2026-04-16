# AnythingTools - Autonomous Multi-Agent Orchestration System

## 1. Project Overview

**What the system does:** An autonomous multi-agent orchestration system that executes complex tasks using web automation, LLM reasoning, and tool composition. The system operates as a background worker accepting REST API requests, spawning specialized agent personas (Scout, Analyst, Quant, Editor, Herald, Archivist, Navigator), and executing tool chains autonomously with loop protection and budget management.

**Concrete operational capabilities:**
- Execute web scraping and data extraction via browser automation (Playwright + Botasaurus framework)
- Perform financial analysis and reconciliation using Yahoo Finance and SEC EDGAR
- Research topics with multi-step reasoning and visual analysis
- Manage vector search and long-term memory operations
- Publish formatted content to external channels
- Automatically condense context when budget exceeds 70%
- Send real-time progress notifications via Telegram
- Persist all state in SQLite with WAL mode for resilience
- Manage human-in-the-loop pauses for CAPTCHA/Cloudflare resolution

**What it explicitly does NOT do:**
- Does not provide a frontend UI (all interactions via API/CLI)
- Does not maintain long-term memory embeddings beyond session lifetime
- Does not implement concurrent job execution (single-writer concurrency model)
- Does not use Chat Completions API (exclusively Azure Responses API)
- Does not provide automatic retry logic for failed LLM calls
- Does not include built-in authentication on API endpoints

## 2. High-Level Architecture

### Major Components

**Agent Core (`bot/core/`):**
- `agent.py`: UnifiedAgent state machine with 50-call hard cap, loop protection, and budget enforcement
- `modes.py`: Seven persona definitions with execution types, system prompts, and allowed tools
- `weaver.py`: Context assembly with 70/20/10 budget enforcement and automatic condensation
- `constants.py`: Centralized tool name constants for mode switching

**LLM Provider Layer (`clients/llm/`):**
- Exclusive Azure Responses API implementation (no streaming, no Chat Completions)
- `payloads.py`: Request construction with automatic low-reasoning effort injection
- `factory.py`: Singleton client wrapper
- `azure.py`: Responses API with `create` endpoint
- `chutes.py`: Fallback provider with identical interface

**Tool System (`tools/`):**
- 25+ tools across 6 categories (browser, library, system, finance, research, publisher)
- Registry-based discovery with `BaseTool` interface
- All tools use `telemetry: Any` parameter (no typed callbacks)
- Phantom Tool support for handling import failures gracefully

**Database Layer (`database/`):**
- Single-writer SQLite with WAL mode
- Tables: `sessions`, `jobs`, `execution_ledger`, `job_logs`, `job_items`, `telemetry_events`
- Background writer thread with queue (`maxsize=1000`)
- Automatic missing-table repair in write path

**Observability (`utils/logger/`):**
- Dual-stream logging (console + master file)
- Telegram integration via `log.dual_log(..., notify_user=True)`
- Debounced debugger agent (3-min cooldown on warnings)
- Tool buffer flush to `job_logs`

**HITL System (`utils/hitl.py`):**
- Standardized human-in-the-loop pause mechanisms
- Browser lock release before pause to prevent deadlocks
- Exception-based flow control for PAUSED_FOR_HITL state

### Data Flow

1. **Job Creation**: POST `/tools/{tool_name}` → DB insert + queue enqueue
2. **Worker Claim**: `UnifiedWorkerManager` polls DB → spawns execution thread
3. **Agent Start**: `UnifiedAgent.run()` → sets `_current_job_id` → sends start notification
4. **Context Build**: `build_session_context()` + `get_session_cost()` with budget check
5. **Budget Check**: If cost > 70% → trigger condensation (stats → summary → delete → insert)
6. **LLM Call**: `await llm.complete_chat(LLMRequest(messages, tools))`
7. **Tool Execution**: `run_tool_safely()` → append to ledger → Telegram notification
8. **Loop**: Continue until 50-call cap, termination, or PAUSED_FOR_HITL

### Execution Model

Event-driven autonomous loops with hard caps. Background writer thread maintains single-writer guarantees. Session-level locking prevents concurrent job execution per session.

## 3. Repository Structure

```
c:/New folder/AnythingTools/
├── app.py                          # FastAPI entry point with lifespan hooks
├── config.py                       # Environment configuration (100+ settings)
├── requirements.txt                # Python dependencies (OpenAI, Playwright, etc.)
├── .env                            # Secrets (not in repo)
├── snowflake_private_key.p8        # Snowflake credentials (if configured)
├── api/
│   ├── routes.py                   # REST endpoints (jobs, status, manifest)
│   ├── schemas.py                  # Pydantic models
│   ├── telegram_client.py          # Telegram Bot with orphan handshake
│   └── telegram_notifier.py        # Legacy wrapper (routes through telegram_client)
├── bot/
│   ├── core/
│   │   ├── agent.py               # UnifiedAgent (main state machine, 235 lines)
│   │   ├── constants.py           # TOOL_* constants
│   │   ├── modes.py               # 7 persona definitions (Scout, Analyst, etc.)
│   │   ├── weaver.py              # Context assembly + budget logic
│   │   └── weaver.py              # Context assembly + budget enforcement
│   ├── engine/
│   │   ├── tool_runner.py         # Error handling wrapper (run_tool_safely)
│   │   └── worker.py              # Job queue consumer with Death Spiral Brake
│   └── capabilities/              # Empty (legacy structure)
├── clients/
│   └── llm/
│       ├── types.py               # LLMRequest/LLMResponse dataclasses
│       ├── payloads.py            # Request builder + reasoning config
│       ├── factory.py             # Singleton client
│       └── providers/
│           ├── azure.py           # Responses API implementation
│           └── chutes.py          # Alternative provider
├── database/
│   ├── schema.py                  # INIT_SCRIPT with all tables (408 lines)
│   ├── writer.py                  # Background writer with repair logic
│   ├── connection.py              # Thread-local connections
│   ├── reader.py                  # Query helpers
│   ├── job_queue.py               # Job status management
│   └── blackboard.py              # (Unused global state vestigial)
├── tools/
│   ├── base.py                    # BaseTool + ToolResult
│   ├── registry.py                # Dynamic import of 25+ tools + Phantom Tool support
│   ├── library_query.py           # Top-level entry point (legacy kept)
│   └── actions/                   # Scoped tool collections
│       ├── system/
│       │   ├── state/tool.py      # system_declare_failure, switch_mode
│       │   ├── files/             # File operations
│       │   └── skills/            # CRUD operations
│       ├── browser/
│       │   ├── browser_operator/  # Playwright-based automation (SoM injection)
│       │   └── macros/            # Saved macro management
│       ├── library/
│       │   ├── vector_search.py   # Vector similarity search
│       │   └── pdf_search/        # PDF parsing tools
│       └── (finance, research, polymarket, quiz, etc.)  # Domain-specific tools
├── utils/
│   ├── logger/                    # Complete logging framework (5 files)
│   │   ├── core.py                # SumAnalLogger + dual_log
│   │   ├── formatters.py          # Payload serialization
│   │   ├── handlers.py            # File handlers
│   │   ├── routing.py             # Specialized file routing
│   │   └── state.py               # ContextVars (_current_job_id, buffers)
│   ├── browser_lock.py            # Asyncio Lock proxy with safe_release()
│   ├── hitl.py                    # HITL pause utilities + exception raising
│   ├── som_utils.py               # SoM injection + DOM stability functions
│   ├── browser_utils.py           # Browser helpers
│   ├── text_processing.py         # HTML cleaning, truncation
│   ├── id_generator.py            # ULID generation
│   ├── budget.py                  # Character cost calculation
│   ├── search_client.py           # Shared DDGS wrapper (Service Locator pattern)
│   └── artifacts.py               # Artifact HTTP URL generation
└── tests/
    └── test_browser_e2e.py        # Minimal browser test
```

**Unconventional structures:**
- No `src/` directory (flat structure)
- Tools split between `actions/` subdirs and top-level legacy entry
- `utils/logger/` is a complete logging framework (not just wrappers)
- `bot/engine/` exists but `bot/core/agent.py` contains main loop
- `database/` has both `writer.py` and `reader.py` but no ORM
- Registry supports Phantom Tools (stub objects for failed imports)

## 4. Core Concepts & Domain Model

### Key Abstractions

**UnifiedAgent**: Re-entrant state machine with:
- Hard cap of 50 tool calls per job
- Loop protection via 3-call repetition breaker
- Budget management (70/20/10 split)
- Automatic context condensation at 70% threshold
- Mode switching capability
- PAUSED_FOR_HITL detection and re-orientation

**Session + Job**: 
- `sessions` table tracks isolation
- `jobs` table tracks execution status (QUEUED, RUNNING, INTERRUPTED, COMPLETED, FAILED, ABANDONED, PAUSED_FOR_HITL)
- `execution_ledger` stores all messages with character costs
- `job_logs` contains runtime log entries

**Context Window**: 
- 70% for history (from ledger)
- 20% for tools (tool descriptions)
- 10% for response (hard-coded in `agent.py`)

**Tool Registry**: 
- Dynamic discovery via `tools/actions/` directory structure
- All tools inherit `BaseTool` with `name` and optional `INPUT_MODEL`
- Phantom Tool support: creates stub on `ImportError` with AST-parsed name
- Manifest generation from module docstrings and `Skill.py` files

**Budget Management**: 
- `get_session_cost()` queries DB and sums character counts
- Condensation triggers when `current_cost > budget * 0.7`
- Process: split ledger → generate summary → delete old rows → insert `<CONDENSED_HISTORY>`

**HITL Pipeline**:
- `pause_for_hitl()` releases browser lock and raises `PAUSED_FOR_HITL` exception
- Worker catches exception and transitions job to `PAUSED_FOR_HITL`
- Agent checks ledger on resume and injects re-orientation prompt
- Exception propagates through all layers without suppression

**Death Spiral Brake**:
- Worker tracks per-job errors in `_system_errors` dict
- After 3 consecutive errors, job marked as `ABANDONED`
- Error counter resets on success
- 10-second sleep between retries for first 2 errors

**Artifact Deep Linking**:
- Tools return `ToolResult` with `attachment_paths`
- Agent converts paths to HTTP URLs using `artifact_relpath_for_http()`
- Links injected into observation text as `<a href="...">`
- Telegram notifications display clickable links

### Domain Model Flow

```python
Job → UnifiedAgent → LLMRequest → LLMResponse → Tool Calls → ToolResult → Append to Ledger
     ↓
  [Budget Check] → [Context Condensation if needed]
     ↓
  [Loop with 50-call cap] → [Death Spiral Brake 3-strike rule]
     ↓
  [Telegram Notifications] ← [log.dual_log(..., notify_user=True)]
```

### Implicit Rules & Invariants

- All tools must inherit `BaseTool` with `name` and `INPUT_MODEL`
- All tool signatures: `async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str`
- Attachment metadata requires `total_char_count` for accurate budgeting
- `system_declare_failure` triggers immediate agent termination
- Three identical tool calls (same name + args) = infinite loop termination
- All LLM calls use `reasoning_effort = "low"` globally
- Browser operations require single-writer lock (`browser_lock`)
- Telegram notifications are fire-and-forget (failures are silent)
- PAUSED_FOR_HITL must never be suppressed by exception handlers

## 5. Detailed Behavior

### Normal Execution Flow

1. **API Request**: POST `/tools/{tool_name}` with JSON args
2. **Validation**: 
   - Check tool exists in registry
   - Validate against tool's `INPUT_MODEL` if present
   - Scan args for URLs (security check)
   - DB insert to `jobs` table (status: QUEUED)
3. **Worker Polling**: `UnifiedWorkerManager` polls every 1s for QUEUED/INTERRUPTED jobs
4. **Thread Spawn**: `spawn_thread_with_context()` creates execution thread
5. **Agent Startup**:
   - Set `_current_job_id` context var
   - Send start notification: `🚀 Job Started: {mode.name}`
   - Check ledger for `PAUSED_FOR_HITL` history → inject re-orientation prompt
6. **Loop Entry**: While `tool_call_count < 50`:
   - **Build Context**: 
     - Query `execution_ledger` for session history
     - Calculate total character cost
     - If `cost > budget * 0.7` → trigger condensation
   - **Budget Check**: If budget exceeded:
     - Split ledger in half (midpoint = len(rows)//2)
     - Generate summary via LLM (max 800 tokens)
     - Delete old rows via `enqueue_write`
     - Insert `<CONDENSED_HISTORY>` message
     - Rebuild context from updated ledger
   - **LLM Call**: 
     - `LLMRequest(messages, tools, reasoning_effort="low")`
     - Azure Responses API `create` endpoint
   - **Response Handling**:
     - No tool_calls → append assistant content → return COMPLETED
     - Tool calls → process each:
       - Extract args, check repetition (name + args checksum)
       - Handle special tools: `switch_mode`, `declare_failure`
       - Execute via `run_tool_safely()` (wraps in try/except)
       - Check output for `PAUSED_FOR_HITL:` prefix
       - Handle artifact paths → convert to HTTP links
       - Append tool result to ledger
       - Send Telegram notification
7. **Loop Continue**: Back to context building
8. **Termination**:
   - 50-call cap reached → Security Intervention
   - `system_declare_failure` → Immediate termination
   - `PAUSED_FOR_HITL` → Job state change + exception propagation
   - Success → COMPLETED with result

### Error Handling

**Tool Execution (`run_tool_safely`)**:
```python
try:
    output = await tool.run(...)
    success = True
except Exception as e:
    output = str(e)  # Full traceback as string
    success = False
return ToolResult(output, success, ...)
```

**PAUSED_FOR_HITL Detection** (post-fix):
```python
tool_result = await run_tool_safely(...)
if "PAUSED_FOR_HITL:" in tool_result.output:
    raise Exception(tool_result.output[...])
```

**Database Writer**:
- `enqueue_write()` adds to queue (maxsize=1000, blocks if full)
- `db_writer_worker()` pops from queue
- On `OperationalError`: attempts missing-table repair
- Repair logic: creates `job_logs` if missing, runs full `INIT_SCRIPT`
- All writes wrapped in transaction, rolled back on error

**Worker Exception Handling**:
```python
try:
    result = await agent.run(...)
except Exception as e:
    if str(e).startswith("PAUSED_FOR_HITL:"):
        # Update job status, append to ledger
        # Do NOT retry
    else:
        self._system_errors[job_id] += 1
        if count >= 3:
            # ABANDONED
        else:
            # INTERRUPTED + 10s sleep
```

**Telegram Notifier**: Failures are silent (logged only)

### Configuration Paths

- `LLM_CONTEXT_CHAR_LIMIT` (default 100,000)
- `AZURE_KEY`, `AZURE_ENDPOINT`, `AZURE_DEPLOYMENT`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID` (dynamic)
- `ALLOW_DESTRUCTIVE_RESET` (env var for schema migration)
- `CHROME_USER_DATA_DIR` (for browser persistence)

## 6. Public Interfaces

### API Endpoints (from `api/routes.py`)

- `POST /tools/{tool_name}`: Create and enqueue job
  - Request body: `{"args": {...}, "client_metadata": {...}}`
  - Response: `{"job_id": "...", "status": "QUEUED"}`
  - Status: 202 Accepted

- `GET /jobs/{job_id}`: Get job status + logs + result
  - Response: `{"status": "...", "logs": [...], "result": {...}}`

- `GET /manifest`: Available tools with schemas
  - Response: `[{name, description, input_schema}, ...]`

- `DELETE /jobs/{job_id}`: Request cancellation (sets flag)

- `GET /health`: Simple health check

- `GET /metrics`: Write queue size + browser health

### Function Signatures

**Tools**:
```python
async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str
```

**UnifiedAgent**:
```python
async def run(self, telemetry: Any, **kwargs) -> Dict[str, Any]
```

**LLM Provider**:
```python
async def complete_chat(self, request: LLMRequest) -> LLMResponse
```

**Tool Registry**:
```python
REGISTRY.load_all()  # Refresh discovery
REGISTRY._tools.get(tool_name)  # Get metadata
REGISTRY.create_tool_instance(name)  # Instantiate
```

### Tool Entry Points

- `tools/library_query.py`: Top-level legacy entry (deprecated but functional)
- `tools/scraper/tool.py`: Scout mode entry with Botasaurus integration
- `tools/finance/tool.py`: Quant mode entry (Yahoo Finance + EDGAR)
- `tools/browser_task/tool.py`: Navigator mode entry (Playwright)

## 7. State, Persistence, and Data

### Storage Locations

- **SQLite**: `data/sumanal.db` (with `-wal`/`-shm` sidecars)
- **Logs**: `logs/` directory (via `utils/logger/routing.py`)
- **Artifacts**: `artifacts/` (mounted as `/artifacts` static files)
- **Temp Files**: `chrome_download/` (browser downloads)

### Critical Schemas

**`execution_ledger`** (core message history):
```sql
CREATE TABLE execution_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_id TEXT UNIQUE NOT NULL,
    job_id TEXT NOT NULL,
    session_id TEXT,
    role TEXT NOT NULL CHECK(role IN ('system','user','assistant','tool')),
    content TEXT NOT NULL,
    attachment_metadata TEXT,  -- JSON, includes char_count
    char_count INTEGER NOT NULL DEFAULT 0,
    attachment_char_count INTEGER NOT NULL DEFAULT 0,
    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX idx_execution_ledger_job_id ON execution_ledger(job_id, id ASC);
```

**`jobs`** (status tracking):
```sql
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING','QUEUED','RUNNING','INTERRUPTED','COMPLETED','FAILED','ABANDONED','CANCELLING','PAUSED_FOR_HITL')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    result_json TEXT,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
CREATE INDEX idx_jobs_session_status ON jobs(session_id, status);
```

**`job_logs`** (runtime observability):
```sql
CREATE TABLE job_logs (
    id INTEGER PRIMARY KEY,
    job_id TEXT,
    tag TEXT,
    level TEXT,
    status_state TEXT,
    message TEXT,
    payload_json TEXT,
    timestamp TEXT
);
```

### Data Lifecycle

- **Session**: Persists until manual purge via `purge_stale_sessions()`
- **Job**: Retained indefinitely (no TTL)
- **Ledger**: Retained unless condensed (old rows deleted)
- **Job Logs**: Flushed on tool completion
- **Debugger Buffer**: Retained with size limit
- **PDF Cache**: Truncated on startup and shutdown

### Migration Strategy

- `database/schema.py` contains `init_db()` and `get_init_script()`
- `ALLOW_DESTRUCTIVE_RESET=1` enables destructive migrations
- Writer thread has automatic repair logic for missing tables
- `PRAGMA user_version` tracking for schema versioning
- Missing tables detected at write-time, repaired automatically

## 8. Dependencies & Integration

### External Libraries

- **`openai>=1.0.0`**: Azure Responses API client (exclusive usage)
- **`playwright`**: Browser automation with DOM manipulation + SoM injection
- **`python-telegram-bot>=21.0`**: Telegram Bot API with retry/flood control
- **`httpx>=0.25.0`**: Async HTTP client for Telegram
- **`sqlite3`**: Core persistence (no external ORM)
- **`ulid-py`**: Time-sorted unique IDs for DB rows
- **`ddgs>=0.4.0`**: DuckDuckGo search wrapper (Service Locator)
- **`botasaurus>=1.0.0`**: Browser automation framework (Playwright wrapper)
- **`pandas>=2.0.0`**: Data analysis
- **`yfinance>=0.2.18`**: Yahoo Finance
- **`edgartools>=2.0.0`**: SEC EDGAR parsing

### Why Each Dependency

- **`openai`**: Azure Responses API exclusive usage (no Chat Completions, no streaming)
- **`playwright`**: Direct DOM access for SoM injection + browser operations
- **`python-telegram-bot`**: Native retry/flood control (better than raw HTTP)
- **`httpx`**: Async Telegram API without blocking
- **`sqlite3`**: Single-writer WAL concurrency, no external deps
- **`ulid-py`**: Time-sorted IDs for DB ordering
- **`ddgs`**: Unified search interface (replaces tool-specific search code)
- **`botasaurus`**: Playwright wrapper with built-in context management

### Coupling Points

- Azure API key must be valid at runtime
- Telegram bot token required for notifications (failures silent)
- Browser requires `playwright` installation (not headless-only)
- SQLite WAL mode requires filesystem write access
- Network access to Azure OpenAI, Telegram API, Google, financial APIs

### Environment Assumptions

- Windows/Linux/WSL (paths work cross-platform)
- Python 3.11+
- No external services required (self-contained)
- Network access to Azure OpenAI, Telegram, Google, Yahoo, SEC

## 9. Setup, Build, and Execution

### Clean Setup

```bash
# 1. Navigate to project
cd c:/New folder/AnythingTools

# 2. Create Python 3.11+ virtual environment
python -m venv .venv
.\.venv\Scripts\activate  # Windows
source .venv/bin/activate  # Linux/Mac

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install playwright browsers
playwright install chromium

# 5. Configure environment
echo "API_KEY=your_api_key" > .env
echo "AZURE_KEY=your_azure_key" >> .env
echo "AZURE_ENDPOINT=https://your-endpoint.openai.azure.com" >> .env
echo "AZURE_DEPLOYMENT=gpt-5.4-mini" >> .env
echo "TELEGRAM_BOT_TOKEN=your_bot_token" >> .env
echo "TELEGRAM_USER_ID=your_user_id" >> .env  # Optional, auto-bind via handshake
echo "CHROME_USER_DATA_DIR=chrome_profile" >> .env

# 6. Run server (DB auto-initializes on first write)
uvicorn app:app --reload --port 8000
```

### Build Process

None; pure Python. No compilation required.

### Execution Constraints

- Must have filesystem write access to `data/`, `logs/`, `artifacts/`
- Must have network access to Azure OpenAI and Telegram API
- Memory usage scales with context size (~100KB-1MB per job)
- CPU: primarily idle, bursts during LLM calls and browser operations
- Disk: WAL files require ~1.5x DB size temporarily during writes

### Testing Setup

```bash
cd tests
python test_browser_e2e.py  # Requires credentials
```

## 10. Testing & Validation

### Existing Tests

- `tests/test_browser_e2e.py`: Single end-to-end browser test
- No unit tests for LLM providers, agent loop, or logger
- No test suite runner or framework configuration
- No pytest markers, fixtures, or mocking

### Coverage Gaps

- Zero tests for agent loop (state machine)
- No tests for ledger budget calculations
- No tests for Telegram integration
- No tests for database writer concurrency
- No tests for tool registry (dynamic loading)
- No tests for Phantom Tool behavior
- No tests for PAUSED_FOR_HITL flow
- No tests for Death Spiral Brake

### Validation Strategy

- Runtime errors are logged + returned as failures
- Telegram notifications provide manual verification
- DB inspection via SQLite CLI possible
- Dual logging provides both console and file trails

## 11. Known Limitations & Non-Goals

### Hard Constraints

- **50 tool call hard cap** (infinite loop protection)
- **No concurrent job execution** per session (single-writer DB)
- **No built-in authentication** on API endpoints
- **No rate limiting** on Telegram notifications
- **No retry logic** for failed LLM calls
- **No Chat Completions API** (Responses API only)
- **No streaming responses** (batch only)

### Technical Debt & Vestigial Artifacts

- `clients/snowflake_client.py`: Exists but no references (dead code)
- `utils/debugger_agent.py`: Exists but only debounces warnings
- `tests/` directory exists but no test runner configuration
- `bot/capabilities/`: Empty init files (legacy structure)
- `database/blackboard.py`: Global state vestigial (not used)
- `api/telegram_notifier.py`: Legacy wrapper (routes through telegram_client)

### Features That Appear Implied But Absent

- Real-time streaming responses (removed, responses API only)
- Long-term memory embeddings (all operations ephemeral per session)
- User authentication/authorization (no auth layer)
- Multi-tenant isolation (implicit via session isolation only)
- Horizontal scaling (single-writer model)
- GraphQL or WebSocket APIs

### Trade-offs

- **Reliability over flexibility**: Single-writer DB with WAL, no concurrency
- **Observability over simplicity**: Deep logging infrastructure (dual streams)
- **Safety over speed**: 50-call cap, explicit condensation, no automatic retries
- **Transparency over privacy**: All state visible in SQLite, no encryption

## 12. Change Sensitivity

### Most Fragile Components

**1. Database Writer (`database/writer.py`)**:
- Missing-table repair logic is critical; failure blocks all writes
- `enqueue_write()` silently drops writes if queue full (size 1000)
- Single-writer thread requires all DB ops through this path
- Changing table schemas without `INIT_SCRIPT` update breaks repair

**2. Agent Loop (`bot/core/agent.py`)**:
- All notifications now via `log.dual_log(..., notify_user=True)`
- Budget calculation depends on `get_session_cost()` querying DB correctly
- Loop protection uses checksum on last 3 tool calls
- PAUSED_FOR_HITL detection logic is fragile (string matching)

**3. Context Weaver (`bot/core/weaver.py`)**:
- `build_session_context()` affects all agent modes
- Budget thresholds (70/20/10) are hard-coded
- Condensation logic deletes old ledger rows (irreversible)

**4. LLM Payloads (`clients/llm/payloads.py`)**:
- `_build_responses_payload()` must match Azure API exactly
- Reasoning config normalization is critical (`reasoning_effort = "low"`)

**5. Registry (`tools/registry.py`)**:
- Phantom Tool logic depends on AST parsing of `name` variable
- Import error handling must distinguish ImportError from other errors
- Discovery logic assumes specific directory structure

**6. Browser Lock (`utils/browser_lock.py`)**:
- `safe_release()` must prevent RuntimeError on unlocked lock
- HITL pauses rely on lock release before exception

### Easy Extension Points

- **Add new tools**: Create file under `tools/actions/{category}/`, inherit `BaseTool`, restart
- **Add modes**: Update `bot/core/modes.py` entries
- **Add DB tables**: Update `INIT_SCRIPT`, repair logic auto-handles missing tables
- **Add LLM providers**: Implement `complete_chat()` in `clients/llm/providers/`

### Hard Extension Points

- **Change to Chat Completions API**: Requires rewriting `clients/llm/providers/azure.py` and `payloads.py`
- **Add concurrency**: Requires migrating away from single-writer SQLite
- **Add auth**: Would require APIGateway pattern or middleware
- **Change database**: Schema, writer logic, and connection management all coupled to SQLite