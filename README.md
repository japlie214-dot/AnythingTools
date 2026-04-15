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
    - Mode switching via `system_switch_mode` tool
    - Programmatic vs Autonomous execution types
    - LLM invocation with tool schema injection
```

#### d) Tool Registry (`tools/registry.py`)
```python
class ToolRegistry:
    - Dynamic discovery of BaseTool subclasses
    - Auto-scans `tools/actions/<scope>/` directories
    - Also attempts to import `tools.actions.<scope>.<module>.tool` for subpackages
    - Extracts schemas from INPUT_MODEL classes
    - Provides MCP-style manifest via schema_list()
    - Validates tool names against Azure OpenAI regex `^[a-zA-Z0-9_-]+$` (no dots)
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
Mode: AUTONOMOUS → Think-Act-Observe loop with LLM:
    1. LLM selects tool from allowed_tools list
    2. Tool executes (possibly multiple steps)
    3. Results written to execution_ledger
    4. Loop repeats until LLM returns success/failure
    ↓
Job marked COMPLETED/FAILED or INTERRUPTED on crash
```

#### Single-Writer Queue Flow
```
Worker Threads → enqueue_write() → write_queue (maxsize=1000)
    ↓
SQLite Writer Thread (database/writer.py) → conn.execute() → conn.commit()
    ↓
write_generation counter increments → wait_for_writes() polls queue depth
```

#### Recovery Flow on Startup
```
app.py startup
    ↓
Query jobs WHERE status IN ('RUNNING', 'INTERRUPTED')
    ↓
UPDATE jobs SET status='FAILED' WHERE status='RUNNING'
    ↓
Re-enqueue INTERRUPTED jobs with 'INTERRUPTED' status
    ↓
Worker Manager claims INTERRUPTED jobs first
    ↓
UnifiedAgent injects recovery message into execution_ledger
    ↓
Normal execution resumes
```

## 3. Repository Structure

### Top-Level Directory Mapping

- `.env` - Environment variables (API keys, DB path, log levels)
- `.gitignore` - Excludes venv, data, logs, private keys
- `app.py` - FastAPI application entrypoint (production startup)
- `config.py` - Centralized config loader (env var parsing, defaults)
- `requirements.txt` - Python dependencies (FastAPI, uvicorn, pandas, etc.)
- `snowflake_private_key.p8` - (Fallback/legacy) unused in current paths
- `.venv/` - Virtual environment (local, excluded from VCS)

### rst
```
.
├── .env                     # Runtime config: DB path, API keys, logging
├── .gitignore              # VCS filters for data, logs, venv, secrets
├── app.py                  # FastAPI entrypoint; starts API server and writer thread
├── config.py               # Central config parsing and defaults
├── requirements.txt        # Project dependencies
├── snowflake_private_key.p8 # Vestigial/legacy secret (unused in current code)
└── .venv/                  # Local virtual environment
```

### `api/`

```
api/
├── __init__.py
├── routes.py              # FastAPI router: POST /api/tools/{tool_name}, GET /api/jobs/{job_id}, DELETE /jobs and /sessions/memory, GET /manifest
├── schemas.py             # Pydantic models: JobCreateRequest, JobCreateResponse, JobStatusResponse, JobLogEntry
└── telegram_notifier.py   # Stub/unused: consider vestigial; no references in active code
```

### `bot/`

```
bot/
├── telemetry.py           # Telemetry hooks (placeholder; not referenced in active execution)
└── capabilities/
    ├── __init__.py
    └── system_tools.py    # REMOVED: Previously contained non-BaseTool implementations; deleted to remove duplication and signature mismatches
└── core/
    ├── __init__.py
    ├── agent.py           # UnifiedAgent: Think-Act-Observe loop with tool call guard and mode switching
    ├── constants.py       # Central tool-name constants (e.g., TOOL_SYSTEM_SWITCH_MODE)
    ├── modes.py           # AgentMode dataclasses defining 6 personas with allowed_tools
    └── weaver.py          # Utility for LLM message formatting (used by agent)
└── engine/
    ├── __init__.py
    ├── tool_runner.py     # Tool execution wrapper (run_tool_safely, len guard)
    └── worker.py          # UnifiedWorkerManager: Polls jobs, claims, spawns threads, recovery
└── orchestrator/
    ├── context.py         # Currently unused; appears to exist for historical or extension purposes
    └── eviction.py        # Job cancellation/gc logic (referenced in routes.py DELETE /jobs)
```

### `clients/`

```
clients/
├── snowflake_client.py    # Unused: no references in active execution paths
└── llm/
    ├── __init__.py        # Re-exports factory, types, utils
    ├── factory.py         # get_llm_client("azure") provider factory
    ├── payloads.py        # Dataclasses for LLM request/response models
    ├── utils.py           # LLM utility helpers
    └── providers/
        ├── __init__.py
        ├── azure.py       # Azure OpenAI client (HTTP requests, token counting)
        └── chutes.py      # Chutes provider (stubbed; not referenced in config defaults)
```

### `database/`

```
database/
├── __init__.py
├── schema.py             # 11-table schema (execution_ledger, jobs, job_items, sessions, ...); init script with migrations
├── connection.py         # DatabaseManager: read/write connection pools, busy_timeout, row_factory
├── writer.py             # Single-writer queue (async-safe); append_to_ledger() helper with char/attachment counting
├── job_queue.py          # Helper functions to enqueue jobs/session rows; add_job_item() with correct typing
├── reader.py             # Helper for async wait_for_writes(); leverages database writer generation counter
├── blackboard.py         # Job step coordination (claim, save, fail) for autonomous loops
├── formula_cache.py      # Financial formula cache used by finance tools
```

### `tools/`

```
tools/
├── __init__.py
├── base.py               # BaseTool abstract class; ToolResult dataclass with attachment paths
├── registry.py           # ToolRegistry; discovers BaseTool subclasses; validates names; adds .tool subpackage import for actions
├── library_query.py      # Legacy top-level tool for library search (public entry point)
└── actions/              # Tool implementations organized by scope
    ├── system/
    │   ├── state/
    │   │   ├── __init__.py
    │   │   └── tool.py   # Implements initialize_checklist, complete_step, switch_mode (correctly typed BaseTool)
    │   ├── files/
    │   │   ├── __init__.py
    │   │   └── tool.py   # list/downloads, read_document, delete_file
    │   ├── skills/
    │   │   ├── __init__.py
    │   │   └── tool.py   # CRUD tools for persistent AI skills
    ├── library/
    │   ├── vector_search.py       # Internal similarity search tool (used by Librarian)
    │   └── pdf_search/
    │       ├── __init__.py
    │       ├── tool.py            # PDF search (similarity + keyword)
    │       └── toc_tool.py        # Table-of-contents extraction tool
    ├── browser/
    │   ├── browser_operator/
    │   │   ├── __init__.py
    │   │   ├── tool.py            # Browser automation tool with macro-driven actions
    │   │   ├── prompt.py          # Prompt templates for operator
    │   │   └── Skill.py           # Skill descriptors (legacy/desc mapping)
    │   └── macros/
    │       ├── __init__.py
    │       └── tool.py            # Save, edit, delete, execute macro tools
    └── (other scopes: finance, research, publisher, search, quiz, polymarket, draft_editor)
│       ├── tool.py or __init__.py  # Each scope uses semantic package layout; some have Skill.py for descriptions
```

### `tests/`

```
tests/
└── test_browser_e2e.py     # End-to-end browser test; likely uses Botasaurus/Playwright (requires env setup)
```

### `utils/`

```
utils/
├── id_generator.py         # Thread-safe monotonic ULID generator (used for ledger_id and some identifiers)
├── logger/                 # Dual logger (console + structured JSON)
│   ├── __init__.py
│   ├── core.py             # get_dual_logger() and routing with status_state/jobs updates
│   ├── formatters.py       # JSON and console formatters
│   ├── handlers.py         # Structured and console handlers
│   ├── routing.py          # Log routing configuration
│   ├── setup.py            # Logger initialization
│   └── state.py            # Logger state (level, config)
├── pdf_utils.py            # PDF extraction (pypdf) with write to DB via enqueue_write
├── vector_search.py        # Embedding generation (OpenAI) + vector table writes
├── vision_utils.py         # Unused: image-based analysis stubs
├── browser_daemon.py       # Driver factory for Botasaurus (get_or_create_driver)
├── browser_lock.py         # Async lock proxy (BrowserLockProxy) for browser concurrency control
├── budget.py               # Token budgeting (unused in active paths)
├── context_helpers.py      # spawn_thread_with_context() used by worker
├── debugger_agent.py       # LLM-based debugger (unused; debug-only)
├── hitl.py                 # Human-in-the-loop stub (unused)
├── prompt_cache.py         # Prompt memoization (unused)
├── security.py             # URL scanning/validation
├── som_utils.py            # Unused: screenshot-of-logic stub
├── source_context.py       # Unused: context building stubs
└── tracker.py              # Structured event tracking stubs
```

## 4. Core Concepts & Domain Model

### Key Abstractions

- **Tool**: An atomic unit of work defined by a BaseTool subclass. Each tool has a `name` and optional `INPUT_MODEL` for schema validation. Tools must be surfaced in `tools/` or `tools/actions/<scope>/` to be discovered.

- **Agent Persona (Mode)**: A named capability set defined in `bot/core/modes.py`. Each persona has an `allowed_tools` list. Modes include: Scout (PROGRAMMATIC), Analyst, Editor, Herald, Quant, Archivist, Navigator (AUTONOMOUS).

- **Job**: A unit of work submitted via API. Stored in the `jobs` table with status: QUEUED, RUNNING, COMPLETED, FAILED, INTERRUPTED, CANCELLING. A job is bound to a `session_id` (caller identity).

- **Execution Ledger**: Append-only table storing every tool call, LLM response, and system message for a job. The column `role` indicates 'user', 'assistant', 'system'. It includes `attachment_metadata` for files.

- **Session & Locking**: The `sessions` table tracks `active_job_id` and `is_busy`. The worker manager enforces caller-level locking using `_active_callers`; a caller cannot enqueue more than one job at a time.

- **Writer Queue**: A single-threaded queue for all DB writes. `enqueue_write()` enqueues SQL tuples. `append_to_ledger()` is the canonical wrapper for ledger inserts (computes char counts and ULID ledger_id). `wait_for_writes()` drains the queue.

- **Recovery**: On startup, RUNNING → FAILED, and INTERRUPTED jobs are reinserted into the queue, causing `UnifiedAgent` to inject a recovery message into the ledger.

### Implicit Rules & Invariants

- Tool names must match `^[a-zA-Z0-9_-]+$` (no dots). This is enforced by `ToolRegistry` regex.
- Only one job per session_id is processed at a time (caller-level locking).
- The `execution_ledger` is the SSSOT; no separate conversation history is maintained.
- Job items use `INTEGER PRIMARY KEY` for `item_id`. ULID cannot be used here; `database/job_queue.py:add_job_item` handles timestamp and integer typing correctly.
- `database/writer.py:append_to_ledger()` handles char counts and attachment cost computation. It must be used for all ledger writes (not raw `enqueue_write` INSERT).
- Search tools return capped results (threshold, k) but do not mutate state.
- Browser tools require an active driver; concurrency is serialized via `browser_lock`.
- Library tools can read/write to vector tables or job items but do not modify job status.
- Agent loop has a 50-tool-call hard cap to prevent infinities.

## 5. Detailed Behavior

### Normal Execution (Autonomous Job)

1. User POSTs `/api/tools/research` with JSON args.
2. Router creates job row with status QUEUED; enqueues to writer, starts the manager, and returns job_id.
3. Worker Manager polls every 1s, claims the job, updates status to RUNNING, spawns a thread.
4. In the thread, `UnifiedAgent` is instantiated with job_id, session_id, and initial mode.
5. Agent loads the mode's `allowed_tools` and calls LLM with tool schemas.
6. LLM selects tool from allowed set; `run_tool_safely` executes it; outputs are written to `execution_ledger`.
7. Loop continues until LLM returns `declare_success` or `human_help`; a hard cap of 50 tool calls may also stop the loop.
8. On completion, job status is set to COMPLETED/FAILED. Final result written to `result_json`.
9. Client poll `GET /api/jobs/{job_id}` to read result.

### Programmatic (Scout) Execution

Similar steps, but mode is PROGRAMMATIC and only a single tool call is allowed before the job completes.

### Error Handling

- **Tool errors**: Captured by `run_tool_safely`; error details are appended to the execution ledger with role=system; job marked FAILED.
- **Writer failures**: SQL exceptions are logged with DB:Writer:Error; rollback occurs; the queue still attempts to drain.
- **LLM failures**: Bad request or provider errors are logged; the agent continues or terminates depending on failure type.
- **Recovery**: INTERRUPTED jobs on restart are re-queued with a recovery message inserted into ledger.

### Config Behavior

- `config.py` loads `.env`.
- Key env vars: `DATABASE_URL`, `API_KEY`, `LOG_LEVEL`, `LOG_FILE`, `LLM_AZURE_*`, `ARTIFACTS_ROOT`.
- `LLM_AZURE_*` controls provider selection; if absent, LLM calls fail and jobs terminate with LLM error.

## 6. Public Interfaces

### FastAPI Endpoints (in `api/routes.py`)

- `POST /api/tools/{tool_name}`
  - Input: JSON body with tool arguments.
  - Output: Job ID with status QUEUED.
  - Security: `x-api-key` header (if configured).

- `GET /api/jobs/{job_id}`
  - Output: Job status, logs, final payload, artifact URLs.

- `DELETE /api/jobs/{job_id}`
  - Marks job CANCELLING; sets cancellation flag for active jobs.

- `DELETE /api/sessions/{session_id}/memory`
  - Clears `execution_ledger` for a session; physical files deleted via `delete_messages_with_files()`.

- `GET /api/manifest`
  - Returns MCP-style schema of discovered tools for UI/code generators.

### Python APIs

- `database.writer.append_to_ledger(job_id, session_id, role, content, attachment_metadata=None) -> ledger_id`
  - Use for all ledger writes; computes char count and attachment cost; enqueues via writer queue.

- `tools.registry.REGISTRY`
  - `load_all()`: Rescans and rebuilds tool map.
  - `get_tool_class(name)`: Returns BaseTool subclass.
  - `schema_list()`: Returns MCP-style manifest.

- `bot.engine.worker.get_manager() -> UnifiedWorkerManager`
  - Singleton worker manager. `.start()` to activate polling.

### CLI Entry Points

- `python app.py` starts FastAPI with uvicorn. No CLI flags; use `.env` for configuration.

## 7. State, Persistence, and Data

### Storage Locations

- **SQLite**: Single database file (default path in `config.py:DATABASE_URL`). 11 tables:
  - `jobs`: job state machine table
  - `execution_ledger`: immutable audit log
  - `job_items`: step-level status for autonomous jobs
  - `sessions`: caller locks and active job mapping
  - `scraped_articles*`: scraper output storage with vector mapping
  - `pdf_parsed_pages*`: extracted PDF text and embeddings
  - `long_term_memories*`: agent memory embeddings
  - `browser_macros`: stored browser step sequences
  - `ai_skills`: persistent skills for agents
  - `financial_formulas`, `calculated_metrics`: finance-specific caches

### Data Formats

- `execution_ledger.content`: arbitrary text from tool/LLM/system.
- `execution_ledger.attachment_metadata`: JSON dict of paths and tokens used for files.
- `job_items.input_data/output_data`: JSON per step.
- `jobs.args_json`: original tool arguments.
- `jobs.result_json`: final payload from agent (success/failure + data).

### Lifecycle

- Jobs remain in DB indefinitely after completion unless manually purged.
- Stale sessions can be purged via `database/writer:purge_stale_sessions(days)` (physically deletes files).
- Manual cleanup script not provided; deletion requires direct DB calls.

## 8. Dependencies & Integration

### External Packages

```txt
FastAPI, uvicorn        # HTTP API server
sqlite3                 # Core persistence (built-in)
pandas, numpy           # Finance calculations, dataframes
aiohttp, httpx          # LLM HTTP calls
openai                  # Azure OpenAI client
pypdf                   # PDF extraction
botasaurus              # Browser automation (used in scraper and utils/browser_daemon)
pydantic                # Input validation in tools and API schemas
```

### Why Each Exists (Evidence)

- `pandas`: Used in finance/ingestion.py, finance/grouper.py, finance/metrics.py for time-series aggregation.
- `botasaurus`: Referenced in `utils/browser_daemon.py:get_or_create_driver` and `tools/scraper`. Required for browser automation.
- `openai`: Azure provider in `clients/llm/providers/azure.py`. Used for embeddings and LLM chat.
- `pypdf`: `utils/pdf_utils.py` uses `PdfReader`.

### Assumptions

- LLM provider is Azure OpenAI (default). Others exist but are not default or tested.
- Browser tools require a headful environment (Playwright/Botasaurus dependencies).
- Network access for LLM, embeddings, and browser automation is required.

## 9. Setup, Build, and Execution

### Prerequisites

- Python >= 3.10
- `pip install -r requirements.txt`
- Browser automation requires Playwright binaries (`playwright install`).
- `.env` must contain:
  - `DATABASE_URL` (SQLite path)
  - `API_KEY` (for endpoints)
  - `LLM_AZURE_*` (endpoint, key, deployment, api_version)
  - `ARTIFACTS_ROOT` (directory for scraper/browser outputs)
  - `LOG_LEVEL`, `LOG_FILE` (optional)

### Steps

1. Create `.env` from `.env.example` if available, or set env vars manually.
2. Run database init on first start (schema auto-initializes).
3. Start server:
   ```bash
   python app.py
   ```
   Or:
   ```bash
   uvicorn app:app --host 0.0.0.0 --port 8000
   ```
4. Use `POST /api/tools/{tool_name}` with `x-api-key` to create jobs.
5. Poll `GET /api/jobs/{job_id}` for status and results.

### Configuration

- `.env` only; no CLI flags.
- Log sink controlled via `LOG_FILE` and `LOG_LEVEL`.

### Platform Constraints

- Windows, macOS, Linux supported.
- Browser automation requires system dependencies for Playwright (may need `playwright install-deps` on Linux).
- SQLite may require WAL support (enabled by default).

## 10. Testing & Validation

### Existing Tests

- `tests/test_browser_e2e.py`: Likely full-stack browser test. Requires:
  - Running app
  - Valid LLM provider
  - Browser environment
- No unit test suite appears present.

### How to Run

```bash
pytest tests/test_browser_e2e.py
```

Expect failures if environment is not fully configured (LLM keys, browser binaries).

### Coverage Gaps

- No unit tests for:
  - `database/writer.py` (queue/ledger)
  - `tools/registry.py` (discovery, validation)
  - `bot/engine/worker.py` (locking, recovery)
  - `bot/core/agent.py` (mode switching, cap enforcement)
- No schema migration tests.

## 11. Known Limitations & Non-Goals

### Hard Constraints

- 50-tool-call cap per autonomous job.
- Size-like enforcement: No explicit max payload size for API.
- Single writer thread; could be a bottleneck under high concurrency.
- No conversation history outside ledger.
- Not designed for direct tool calls; job queue required.

### Implied Features Not Implemented

- No built-in user interface, CLI tool runner, or batch ingestion job creator.
- No automatic migration of legacy data.
- No RBAC or fine-grained permissions (API key only).
- No metrics persistence beyond runtime (only in-memory via `/metrics`).

### Technical Debt

- Vestigial modules: `clients/snowflake_client.py`, `utils/vision_utils.py`, `utils/debugger_agent.py`, `utils/hitl.py`, `api/telegram_notifier.py`.
- Duplicate system tools were removed (see Changes section) but some referenced names might remain in legacy docs.

## 12. Change Sensitivity (`bot/core/modes.py`, `tools/registry.py` especially)

### Fragile Areas

- **Tool name changes**: `modes.py` `allowed_tools` lists must match `registry.py` discovery exactly. Mistype or rename causes "Allowed tool ... not found in registry" warning; agents silently fail to call missing tools.

- **Database schema**: `database/schema.py` has minimal migrations. Any new table/column requires manual SQL patches. The app assumes a stable schema post-init.

- **Writer queue**: `database/writer.py` is central; any error or deadlock will stall all DB operations.

- **Agent loop latency**: LLM calls in `UnifiedAgent` are synchronous; the thread is blocked until tool completes.

- **Browser automation**: `botasaurus` driver state is global; improper cleanup can leave orphaned processes.

### Easiest to Extend

- New tools: add a `BaseTool` subclass in any `tools/actions/<scope>/tool.py`, ensuring `name` matches a mode's `allowed_tools`.
- New modes: add entry to `modes.py` `MODES` dict; update API to accept new mode or keep fixed.

### Hardest to Change

- Switching away from SQLite (motivation: single-writer, WAL semantics, busy_timeout assumptions).
- Changing tool discovery pattern (we rely on `tools/` and `tools/actions/<scope>/` paths).
- Changing the 50-call cap or locking strategy (central to safety).