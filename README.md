# AnythingTools - Autonomous Multi-Agent Orchestration System

## 1. Project Overview

**What the system does:** An autonomous multi-agent orchestration system that executes complex tasks using web automation, LLN reasoning, and tool composition. The system operates as a background worker accepting REST API requests, spawning specialized agent personas (Scout, Analyst, Quant, Editor, Herald, Archivist, Navigator), and executing tool chains autonomously with loop protection and budget management.

**Concrete operational capabilities:**
- Execute web scraping and data extraction via browser automation
- Perform financial analysis and reconciliation
- Research topics with multi-step reasoning
- Manage long-term memory and vector search
- Publish formatted content to external channels
- Automatically condense context when budget exceeds 70%
- Send real-time progress notifications via Telegram
- Persist all state in SQLite with WAL mode for resilience

**What it explicitly does NOT do:**
- Does not provide a frontend UI (all interactions via API/CLI)
- Does not maintain long-term memory embeddings (all operations are ephemeral per session)
- Does not implement concurrent job execution (single-writer concurrency model)
- Does not use Chat Completions API (exclusively Azure Responses API)

## 2. High-Level Architecture

### Major Components

**Agent Core (`bot/core/`):**
- `agent.py`: UnifiedAgent state machine with 50-call hard cap and loop protection
- `modes.py`: Six persona definitions with allowed tools and system prompts
- `weaver.py`: Context assembly with 70/20/10 budget enforcement
- `constants.py`: Centralized tool name constants

**LLM Provider Layer (`clients/llm/`):**
- Exclusive Azure Responses API implementation (no streaming)
- `payloads.py`: Request construction with automatic low-reasoning injection
- `factory.py`: Singleton client wrapper
- `azure.py`: Responses API with `create` endpoint
- `chutes.py`: Fallback provider with identical interface

**Tool System (`tools/`):**
- 25+ tools across 6 categories (browser, library, system, finance, research, publisher)
- Registry-based discovery with `BaseTool` interface
- All tools use `telemetry: Any` parameter (no callbacks)

**Database Layer (`database/`):**
- Single-writer SQLite with WAL mode
- Tables: `sessions`, `jobs`, `execution_ledger`, `job_logs`, `job_items`
- Background writer thread with queue (`maxsize=1000`)
- Automatic missing-table repair in write path

**Observability (`utils/logger/`):**
- Dual-stream logging (console + master file)
- Telegram integration via `log.dual_log(..., notify_user=True)`
- Debounced debugger agent (3-min cooldown)
- Tool buffer flush to `job_logs`

### Data Flow

1. API receives job request → creates job in DB
2. Worker thread claims job → spawns UnifiedAgent
3. Agent builds context from `execution_ledger` → calls LLM
4. LLM returns tool calls → agent executes via `run_tool_safely`
5. Tool results appended to ledger → loop continues
6. On completion/failure → Telegram notification + DB update

### Execution Model
Event-driven autonomous loops with hard caps. Background writer thread handles all DB writes sequentially to maintain single-writer guarantees.

## 3. Repository Structure

```
c:/New folder/AnythingTools/
├── app.py                          # FastAPI entry point (implied by routes)
├── config.py                       # Environment configuration
├── requirements.txt                # Python dependencies
├── .env                            # Secrets (not in repo)
├── api/
│   ├── routes.py                   # REST endpoints (jobs, status, manifest)
│   ├── schemas.py                  # Pydantic models
│   └── telegram_notifier.py        # HTTP-based Telegram dispatcher
├── bot/
│   ├── core/
│   │   ├── agent.py               # UnifiedAgent (main state machine)
│   │   ├── constants.py           # TOOL_* constants
│   │   ├── modes.py               # 6 persona definitions
│   │   └── weaver.py              # Context assembly + budget logic
│   ├── engine/
│   │   ├── tool_runner.py         # Error handling wrapper
│   │   └── worker.py              # Job queue consumer
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
│   ├── schema.py                  # INIT_SCRIPT with job_logs table
│   ├── writer.py                  # Background writer with repair logic
│   ├── connection.py              # Thread-local connections
│   ├── reader.py                  # Query helpers
│   ├── job_queue.py               # Job status management
│   └── blackboard.py              # (Unused global state)
├── tools/
│   ├── base.py                    # BaseTool + ToolResult (no TelemetryCallback)
│   ├── registry.py                # Dynamic import of tools
│   ├── library_query.py           # Top-level entry point (legacy kept)
│   └── actions/                   # Scoped tool collections
│       ├── system/
│       │   ├── state/tool.py      # system_declare_failure, switch_mode
│       │   ├── files/             # File operations
│       │   └── skills/            # CRUD operations
│       ├── browser/
│       │   ├── browser_operator/  # Playwright-based automation
│       │   └── macros/            # Saved macro management
│       ├── library/
│       │   ├── vector_search.py   # Vector similarity search
│       │   └── pdf_search/        # PDF parsing tools
│       └── (finance, research, etc.)  # Domain-specific tools
├── utils/
│   ├── logger/
│   │   ├── core.py                # SumAnalLogger + dual_log
│   │   ├── formatters.py          # Payload serialization
│   │   ├── handlers.py            # File handlers
│   │   ├── routing.py             # Specialized file routing
│   │   └── state.py               # ContextVars (_current_job_id, buffers)
│   ├── som_utils.py               # SoM injection + DOM stability functions
│   ├── browser_utils.py           # Browser helpers
│   ├── text_processing.py         # HTML cleaning, truncation
│   ├── id_generator.py            # ULID generation
│   └── budget.py                  # Character cost calculation
└── tests/
    └── test_browser_e2e.py        # Minimal browser test
```

**Unconventional structures:**
- No `src/` directory
- Tools split between `actions/` subdirs and top-level legacy entry
- `utils/logger/` is a complete logging framework
- `bot/engine/` exists but `bot/core/agent.py` contains main loop
- `database/` has both `writer.py` and `reader.py` but no ORM

## 4. Core Concepts & Domain Model

### Key Abstractions

**UnifiedAgent**: Re-entrant state machine with 50-call hard cap, mode switching, and automatic context condensation at 70% budget.

**Session + Job**: `sessions` table tracks isolation; `jobs` table tracks execution status; `execution_ledger` stores all messages with character costs.

**Context Window**: 70% for history, 20% for tools, 10% for response (hard-coded in `agent.py` at budget check).

**Tool Registry**: Dynamic discovery via `tools/actions/` directory structure; all tools must inherit `BaseTool` with `name` and `INPUT_MODEL`.

**Budget Management**: `get_session_cost()` queries DB; condensation triggers when `current_cost > budget * 0.7`.

**Observability**: All notifications via `log.dual_log(..., notify_user=True)` → Telegram + DB + Console.

### Domain Model Flow
```python
Job → UnifiedAgent → LLMRequest → LLMResponse → Tool Calls → ToolResult → Append to Ledger
```

### Implicit Rules
- All tools must inherit `BaseTool` with `name` and `INPUT_MODEL`
- All tool signatures: `async run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str`
- Attachment metadata must include `total_char_cost` for accurate budgeting
- `system_declare_failure` triggers immediate agent termination
- Three identical tool calls = infinite loop termination
- All LLM calls use `reasoning_effort = "low"` globally

## 5. Detailed Behavior

### Normal Execution Flow

1. **Job Creation**: POST `/tools/{tool_name}` → DB insert + queue enqueue
2. **Agent Start**: Worker calls `UnifiedAgent.run()` → sets `_current_job_id` → sends start notification
3. **Context Building**: `build_session_context(session_id, system_prompt, budget)` + `get_session_cost()`
4. **Budget Check**: If `cost > budget * 0.7` → trigger condensation:
   - Split ledger in half
   - Generate summary via LLM
   - Delete old rows
   - Insert `<CONDENSED_HISTORY>` message
5. **LLM Call**: `await llm.complete_chat(LLMRequest(messages, tools))`
6. **Response Handling**:
   - No tool_calls → append assistant content → return COMPLETED
   - Tool calls → process each:
     - Extract args, check repetition breaker
     - Handle special tools (switch_mode, declare_failure)
     - Execute via `run_tool_safely()` (catches exceptions → returns error string)
     - Append tool result to ledger
     - Send Telegram notification
7. **Loop**: Continue until 50-call cap or termination

### Error Handling

- `run_tool_safely()`: Catches exceptions → `ToolResult(output="Traceback", success=False)`
- `db_writer_worker()`: Catches OperationalError → attempts missing-table repair (create `job_logs`, run full init script)
- Telegram notifier failures are silent (logged only)
- All DB writes rolled back on error

### Configuration Paths

- `LLM_CONTEXT_CHAR_LIMIT` (default 100,000)
- `AZURE_KEY`, `AZURE_ENDPOINT`, `AZURE_DEPLOYMENT`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID`
- `ALLOW_DESTRUCTIVE_RESET` (env var for schema migration)

## 6. Public Interfaces

### API Endpoints (from `api/routes.py`)
- `POST /tools/{tool_name}`: Create and enqueue job
- `GET /jobs/{job_id}`: Get status + logs + final payload
- `GET /manifest`: Available tools
- `DELETE /jobs/{job_id}`: Request cancellation

### Tool Entry Points
- `tools/library_query.py`: Top-level legacy entry
- `tools/scraper/tool.py`: Scout mode entry
- `tools/finance/tool.py`: Quant mode entry
- `tools/browser_task/tool.py`: Navigator mode entry

### Function Signatures
```python
# Tools
async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str

# UnifiedAgent
async def run(self, telemetry: Any, **kwargs) -> Dict[str, Any]

# LLM Provider
async def complete_chat(self, request: LLMRequest) -> LLMResponse
```

## 7. State, Persistence, and Data

### Storage Locations
- SQLite: `data/sumanal.db` (with `-wal`/`-shm` sidecars)
- Logs: `logs/` directory (via `utils/logger/routing.py`)
- Temp files: `data/temp/` (screenshots, uploads)

### Critical Schemas

```sql
CREATE TABLE execution_ledger (
    id INTEGER PRIMARY KEY,
    ledger_id TEXT UNIQUE,
    job_id TEXT,
    session_id TEXT,
    role TEXT CHECK(role IN ('system','user','assistant','tool')),
    content TEXT,
    attachment_metadata TEXT,  -- JSON
    char_count INTEGER,
    attachment_char_count INTEGER,
    timestamp TEXT
);

CREATE TABLE job_logs (
    id TEXT PRIMARY KEY,
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
- Session persists until manual purge via `purge_stale_sessions()`
- Job entries retained indefinitely
- Ledger entries retained unless condensed (old rows deleted)
- Tool buffers flushed to `job_logs` on tool completion
- Debugger buffer retained (size-limited)

### Migration Strategy
- `database/schema.py` contains `init_db()` and `get_init_script()`
- `ALLOW_DESTRUCTIVE_RESET=1` enables destructive migrations
- Writer thread has automatic repair logic for missing tables
- `PRAGMA user_version` tracking for schema versioning

## 8. Dependencies & Integration

### External Libraries

- `openai`: Azure Responses API client (exclusive usage)
- `playwright`: Browser automation with DOM manipulation
- `httpx`: Async HTTP client for Telegram
- `sqlite3`: Core persistence (no external ORM)
- `ulid-py`: ID generation
- `python-multipart`: File uploads (via FastAPI)

### Why Each Dependency

- `openai`: Azure Responses API exclusive usage (no Chat Completions, no streaming)
- `playwright`: Direct DOM access for SoM injection + browser operations
- `httpx`: Async Telegram API without blocking
- `sqlite3`: Chosen for single-writer WAL concurrency
- `ulid-py`: Time-sorted unique IDs for DB rows

### Coupling Points

- Azure API key must be available at runtime
- Telegram bot token must be valid (failures are silent)
- Browser requires `playwright` installation (not headless-only)
- SQLite WAL mode requires filesystem write access

### Environment Assumptions
- Windows/Linux/WSL (paths work cross-platform)
- Python 3.11+
- No external services required (self-contained)
- Network access to Azure OpenAI and Telegram API

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
echo "AZURE_KEY=your_azure_key" > .env
echo "AZURE_ENDPOINT=https://your-endpoint.openai.azure.com" >> .env
echo "AZURE_DEPLOYMENT=gpt-5.4-mini" >> .env
echo "TELEGRAM_BOT_TOKEN=your_bot_token" >> .env
echo "TELEGRAM_USER_ID=your_user_id" >> .env

# 6. Run server (DB auto-initializes on first write)
uvicorn app:app --reload --port 8000
```

### Build Process
None; pure Python. No compilation required.

### Execution Constraints
- Must have filesystem write access to `data/` and `logs/`
- Must have network access to Azure OpenAI and Telegram API
- Memory usage scales with context size (~100KB-1MB per job)
- CPU: primarily idle, bursts during LLM calls and browser ops

## 10. Testing & Validation

### Existing Tests
- `tests/test_browser_e2e.py`: Single end-to-end browser test
- No unit tests for LLM providers, agent, or logger
- No test suite runner or framework (no pytest markers)

### How to Run Tests
```bash
cd tests
python test_browser_e2e.py  # Requires browser + credentials
```

### Coverage Gaps
- Zero tests for agent loop (state machine)
- No tests for ledger budget calculations
- No tests for Telegram integration
- No tests for database writer concurrency
- No tests for tool registry

### Validation Strategy
- Runtime errors are logged + returned as failures
- Telegram notifications provide manual verification path
- DB inspection via SQLite CLI possible

## 11. Known Limitations & Non-Goals

### Hard Constraints
- 50 tool call hard cap (infinite loop protection)
- No concurrent job execution for same session (single-writer DB)
- No built-in authentication on API endpoints
- No rate limiting on Telegram notifications
- No retry logic for failed LLM calls

### Technical Debt & Vestigial Artifacts
- `clients/snowflake_client.py`: Exists but has no references (dead code)
- `utils/debugger_agent.py`: Exists but no evidence of active use (debounce only on warnings)
- `tests/` directory exists but no test runner configuration
- `bot/capabilities/` : Empty init files (legacy structure)

### Features That Appear Implied But Absent
- Real-time streaming responses (removed, responses API only)
- Long-term memory embeddings (all operations ephemeral per session)
- User authentication/authorization (no auth layer)
- Multi-tenant isolation (implicit via session isolation only)
- Horizontal scaling (single-writer model)
- GraphQL or WebSocket APIs

### Trade-offs
- **Reliability over flexibility**: Single-writer DB with WAL, no concurrency
- **Observability over simplicity**: Deep logging infrastructure
- **Safety over speed**: 50-call cap, explicit condensation, no automatic retries

## 12. Change Sensitivity

### Most Fragile Components

1. **Database Writer (`database/writer.py`)**:
   - Missing-table repair logic is critical; failure blocks all writes
   - `enqueue_write()` silently drops writes if queue full
   - Single-writer thread requires all DB ops through this path

2. **Agent Loop (`bot/core/agent.py`)**:
   - All notifications now via `log.dual_log(..., notify_user=True)` (was direct `send_notification()`)
   - Budget calculation depends on `get_session_cost()` querying DB correctly
   - Loop protection uses checksum on last 3 tool calls

3. **Context Weaver (`bot/core/weaver.py`)**:
   - `build_session_context()` affects all agent modes
   - Budget thresholds (70/20/10) are hard-coded, not configurable
   - Condensation logic deletes old ledger rows (irreversible)

4. **LLM Payloads (`clients/llm/payloads.py`)**:
   - `_build_responses_payload()` must match Azure API exactly
   - Reasoning config normalization is critical (`reasoning_effort = "low"`)

### Easy Extension Points
- **Add new tools**: Create file under `tools/actions/{category}/`, inherit `BaseTool`, restart
- **Add modes**: Update `bot/core/modes.py` entries
- **Add DB tables**: Update `INIT_SCRIPT`, repair logic auto-handles missing tables

### Hard Extension Points
- **Change to Chat Completions API**: Requires rewriting `clients/llm/providers/azure.py` and `payloads.py`
- **Add concurrency**: Requires migrating away from single-writer SQLite
- **Add auth**: Would require APIGateway pattern or middleware