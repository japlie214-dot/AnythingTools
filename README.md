# AnythingTools

## 1. Project Overview

**Operational Purpose:**  
AnythingTools is a FastAPI-based backend system that orchestrates AI-powered tools for web scraping, document processing, and research automation. It executes tasks as batched "jobs" through a worker manager, persists all state in SQLite databases, and provides REST APIs for job management.

**Concrete Capabilities:**
- Receives tool execution requests via REST API (`POST /api/tools/{tool}`)
- Offloads work to background workers managed by `bot.engine.worker`
- Persists job state, logs, and results in dual SQLite databases (`data/sumanal.db` and `data/logs.db`)
- Supports browser automation via Chrome/Chromium, with automatic zombie process cleanup
- Provides Telegram notifications for job status and user alerts
- Implements backup/export functionality using Parquet format
- Handles vector search via `sqlite-vec` (optional binary extension)
- Supports LLM integration through Azure OpenAI or Chutes API

**Explicit Non-Goals:**
- Does NOT provide a frontend UI (API-only)
- Does NOT implement real-time streaming of job results (pull-based status polling only)
- Does NOT support horizontal scaling (single-writer SQLite architecture)
- Does NOT include persistent chat memory beyond SQLite tables
- Does NOT implement user authentication beyond simple API key validation
- Does NOT handle complex job dependencies or DAGs

## 2. High-Level Architecture

**System Components:**

1. **API Layer (`api/routes.py`)**
   - FastAPI router mounted at `/api`
   - Requires API key header `X-API-Key` for all routes except `/manifest`
   - Endpoints: job enqueue, status retrieval, backup control, manifest
   - All endpoints write to databases via async writer threads

2. **Database Layer (`database/`)**
   - **Dual Database Architecture:**
     - `sumanal.db`: Operational data (jobs, tool results, financial data, vectors, documents)
     - `logs.db`: High-volume telemetry and execution logs (high-throughput, `synchronous=OFF`)
   - **Management Layer (`database/management/`):**
     - `reconciler.py`: Agnostic schema validator that compares DB state against provided schemas
     - `lifecycle.py`: Orchestrates multi-DB validation (`Operational DB` + `Logs DB`)
     - `health.py`: Agnostic health checks and orphaned backup restoration
   - **Writer Architecture:**
     - Single-writer threads per database (separate for main and logs)
     - WAL mode enabled for concurrent readers
     - Bounded queues (max 2000 entries) to prevent memory exhaustion
     - Write generation counters for read connection refresh

3. **Worker Engine (`bot/engine/`)**
   - `worker.py`: `JobManager` that polls `jobs` table and dispatches to tool runners
   - `tool_runner.py`: Executes registered tools with isolated contexts
   - Maintains cancellation flags per job ID

4. **Tool Registry (`tools/`)**
   - Dynamic discovery of tools via `tools/registry.py`
   - Tools are modules with `execute()` function and optional `INPUT_MODEL`
   - Includes: `scraper`, `draft_editor`, `publisher`, `batch_reader`

5. **Browser Automation (`utils/browser_daemon.py`)**
   - Manages Chrome/Chromium lifecycle
   - Detects zombie processes and kills them on startup
   - Supports warmup for faster first request

6. **Logging System (`utils/logger/`)**
   - Dual-stream logger: console output + structured file logs
   - Tags every log entry with `tag` and `level`
   - Routes to both `logs.db` and specialized files
   - Context-aware: automatically tags with job_id when in job context

**Control Flow (Job Execution):**

```
1. Client → POST /api/tools/{tool}
2. API writes job to sumanal.db
3. JobManager poller detects new job
4. ToolRunner executes tool with context
5. Tool writes logs to logs.db
6. Tool writes results to sumanal.db
7. JobManager updates status
8. (Optional) Telegram notification sent
```

**Execution Model:**  
Event-driven via polling loop (job manager checks `jobs` table every 5 seconds), with async writer threads for non-blocking DB operations.

## 3. Repository Structure

```
AnythingTools/
├── app.py                           # FastAPI entrypoint with lifespan hooks
├── config.py                        # Environment-based configuration
├── requirements.txt                 # Python dependencies
├── snowflake_private_key.p8         # Snowflake auth (optional)
├── .env                             # Environment variables
│
├── api/
│   ├── __init__.py
│   ├── routes.py                    # All REST endpoints
│   └── schemas.py                   # Pydantic models for API
│
├── bot/
│   ├── core/                        # Base classes
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── worker.py                # JobManager (polls DB, dispatches)
│   │   └── tool_runner.py           # Tool execution context
│   └── orchestrator_core/           # (Unused in current code)
│
├── tools/
│   ├── __init__.py
│   ├── base.py                      # Tool base class
│   ├── registry.py                  # Dynamic tool discovery
│   ├── scraper/                     # Web scraping tool
│   ├── draft_editor/                # Document editing
│   ├── publisher/                   # Content publishing
│   └── batch_reader/                # Batch processing
│
├── database/
│   ├── __init__.py
│   ├── connection.py                # DatabaseManager + LogsDatabaseManager
│   ├── writer.py                    # Single-writer thread + queues
│   ├── logs_writer.py               # Dedicated logs writer
│   ├── reader.py                    # Query helpers
│   │
│   ├── management/                  # Agnostic DB management (NEW)
│   │   ├── __init__.py              # Exports: SchemaReconciler, health funcs
│   │   ├── reconciler.py            # Schema-agnostic validator
│   │   ├── lifecycle.py             # Multi-DB coordinator
│   │   ├── health.py                # File state checks, restore
│   │   └── schema_introspector.py   # PRAGMA helpers, type affinity
│   │
│   ├── schemas/
│   │   ├── __init__.py              # Aggregated schema metadata
│   │   ├── jobs.py                  # Jobs, job_items, broadcast_batches
│   │   ├── finance.py               # Financial metrics, stock prices
│   │   ├── vector.py                # Scraped articles, long-term memories (+ FTS5)
│   │   ├── pdf.py                   # PDF parsed pages
│   │   ├── token.py                 # Token usage metrics
│   │   └── logs.py                  # Logs table (no FK, separate DB)
│   │
│   ├── backup/
│   │   ├── __init__.py
│   │   ├── config.py                # Backup settings
│   │   ├── exporter.py              # Parquet export logic
│   │   ├── restore.py               # Backup restoration
│   │   ├── runner.py                # Backup orchestrator
│   │   ├── schema.py                # Parquet schemas
│   │   └── storage.py               # File I/O for backups
│   │
│   └── [legacy files in root]
│       ├── blackboard.py            # (Unused/legacy)
│       ├── formula_cache.py         # (Unused/legacy)
│       ├── job_queue.py             # (Unused/legacy)
│       └── reader.py                # (Legacy query helpers)
│
├── utils/
│   ├── logger/
│   │   ├── __init__.py
│   │   ├── core.py                  # Dual-logger class
│   │   ├── handlers.py              # File handlers
│   │   ├── routing.py               # Log file routing
│   │   ├── state.py                 # ContextVars for job_id
│   │   └── formatters.py            # Log record formatting
│   │
│   ├── startup/
│   │   ├── __init__.py              # Orchestrator entrypoint
│   │   ├── core.py                  # StartupOrchestrator class
│   │   ├── database.py              # DB init, migrations, vec0 validation
│   │   ├── cleanup.py               # Chrome zombie kill, temp file removal
│   │   ├── server.py                # Artifact mounting
│   │   ├── registry.py              # Tool registry preload
│   │   ├── browser.py               # Chrome warmup
│   │   └── recovery.py              # Startup recovery logic
│   │
│   ├── browser_daemon.py            # Chrome lifecycle manager
│   ├── browser_lock.py              # Singleton lock for browser ops
│   ├── artifact_manager.py          # Upload/download helper
│   ├── security.py                  # URL SSRF scanning
│   ├── context_helpers.py           # Job context management
│   ├── id_generator.py              # ULID generator
│   └── [many other utility modules]
│
├── deprecated/                      # Legacy code (see Changes section)
│   └── ...
│
└── tests/                           # (Minimal, see Testing section)
```

**Unconventional Structures:**

- **Two-level database management:** Root `database/` contains legacy files; new logic lives in `database/management/` with strict agnostic design
- **Separate logs database:** `logs.db` is isolated for performance, with `synchronous=OFF` and WAL
- **Startup orchestrator:** Tiered execution in `utils/startup/` with dependency management
- **Orphaned backup detection:** Database files with `.db.bak` suffix are auto-restored on startup
- **Deprecated folder:** Contains legacy implementations used for historical inference

## 4. Core Concepts & Domain Model

**Database Schemas:**

*See `database/schemas/` for complete DDL*

**Key Tables (sumanal.db):**
- `jobs`: Job lifecycle (`PENDING` → `QUEUED` → `RUNNING` → `COMPLETED`/`FAILED`)
- `job_items`: Sub-tasks within jobs, with metadata JSON
- `scraped_articles`: Web content, linked to FTS5 virtual table `scraped_articles_fts`
- `scraped_articles_vec`: Vector store for article embeddings (virtual, `vec0`)
- `long_term_memories`: Agent knowledge, linked to `long_term_memories_vec`
- `financial_metrics`, `stock_prices`, `raw_fundamentals`: Financial data
- `pdf_parsed_pages`: Extracted PDF text, linked to `pdf_parsed_pages_vec`
- `token_usage`: LLM token audit trail

**Key Tables (logs.db):**
- `logs`: High-volume telemetry
  - Columns: `id`, `job_id`, `tag`, `level`, `status_state`, `message`, `payload_json`, `timestamp`
  - No foreign keys (performance)

**Virtual Tables:**
- `scraped_articles_fts`: FTS5 full-text search index on `scraped_articles`
- `*_vec`: vec0 vector embedding tables (`scraped_articles_vec`, `long_term_memories_vec`, `pdf_parsed_pages_vec`)
- Shadow tables automatically created: `*_vec_chunks`, `*_vec_rowids`, `*_vec_nodes`, `*_vec_info`

**Implicit Rules:**
1. **Dual-Database Separation:** Logs never mix with operational data
2. **No Hardcoded Schemas in Management:** `SchemaReconciler` takes schemas as arguments
3. **Virtual Table Immunity:** Column validation skips virtual tables entirely
4. **Fail-Open Snapshots:** Corrupted master tables are reset, not fatal
5. **FSH Shadow Whitelisting:** Both FTS5 (`_data`, `_idx`, `_docsize`, `_config`, `_content`) and vec0 (`_chunks`, `_rowids`, `_nodes`, `_info`) shadows are protected

## 5. Detailed Behavior

**Normal Execution (Job Flow):**

1. **Startup (`app.py lifespan`):**
   - Tier 1: Mount artifacts, cleanup Chrome/zombies, init DB writers
   - Tier 2: Run database lifecycle (validate/reconcile both DBs), validate vec0, startup recovery
   - Tier 3: Load tool registry, warmup browser

2. **Job Enqueue (`POST /api/tools/{tool}`):**
   - Validate input against tool's `INPUT_MODEL` (if present)
   - Write `jobs` record with `status=QUEUED`
   - Return `job_id` to client

3. **Job Execution (`bot.engine.worker`):**
   - Poller loop: SELECT unprocessed jobs every ~5s
   - Set `status=RUNNING`, dispatch to `ToolRunner`
   - Tool executes with access to `context_helpers`, `browser_daemon`
   - Tool writes logs → `logs_enqueue_write()` → `logs.db`
   - Tool writes results → `enqueue_write()` → `sumanal.db`
   - Update job status, emit Telegram (if configured)

4. **Shutdown (`app.py lifespan`):**
   - Stop poller, broadcast cancellation flags
   - Drain active jobs (60s timeout)
   - Kill browser, flush DB writers
   - Exit cleanly

**Edge Cases & Error Handling:**

- **Corrupted Database:** Startup fails unless `SUMANAL_ALLOW_SCHEMA_RESET=1`; then it drops and recreates
- **Missing sqlite-vec:** VEC tables created as regular tables with BLOB column; vector functionality disabled
- **Orphaned Backup (`.db.bak`):** Auto-detected and restored on startup; raises `CRITICAL` if 0 bytes
- **Virtual Table Corruption:** `_snapshot_master()` catches `OperationalError`, logs `CRITICAL`, allows DROP/CREATE
- **Queue Full (`maxsize=2000`):** Log writes are dropped silently (non-blocking)
- **Browser Zombie:** Detected by port scanning; killed on startup before warmup

**Configuration:**

All config lives in `config.py`, sourced from `.env`. Key flags:
- `SUMANAL_ALLOW_SCHEMA_RESET=1`: Allows destructive reset on corruption/version mismatch
- `BACKUP_ENABLED=false`: Disables Parquet backup system
- `TELEGRAM_BOT_TOKEN`: Enables notification system

## 6. Public Interfaces

**REST API (`/api`):**

- `GET /` → Health/version
- `POST /api/tools/{tool_name}` → Enqueue job
  - Body: `{"args": {...}, "client_metadata": {...}}`
  - Returns: `{"job_id": "...", "status": "QUEUED"}`
  - Status: `202 Accepted`
- `GET /api/jobs/{job_id}` → Status + logs
  - Returns: `{"job_id": "...", "status": "...", "job_logs": [...], "final_payload": {...}}`
- `DELETE /api/jobs/{job_id}` → Cancel job (sets `CANCELLING` state)
- `GET /api/manifest` → Public, no auth: lists available tools
- `POST /api/backup/export` → Trigger Parquet export (mode: full/delta)
- `GET /api/backup/status` → Backup state (watermark, file counts)
- `POST /api/backup/restore` → Restore from backup (requires browser lock)

**CLI / Manual:**

- `python -m uvicorn app:app --reload --port 8000` → Run API
- `python -c "from database.management.reconciler import SchemaReconciler; ..."` → Manual reconciliation (see README in repo root)

**Tool Interface (`tools/base.py`):**

```python
# Tools implement:
def execute(context, args) -> dict:
    # context provides: logger, job_id, database access helpers
    # args is validated dict
    # Returns dict stored as job final_payload
```

**Environment Variables:**

```bash
# Required for operation
API_KEY=dev_default_key_change_me_in_production

# Optional but common
SUMANAL_ALLOW_SCHEMA_RESET=1    # Safety for dev
TELEGRAM_BOT_TOKEN=...          # Notifications
CHROME_USER_DATA_DIR=...        # Chrome profile persistence
```

## 7. State, Persistence, and Data

**Storage Locations:**

- `data/sumanal.db` -> Operational database (WAL mode)
- `data/logs.db` -> High-throughput logs (WAL, `synchronous=OFF`)
- `data/*.db.bak` -> Orphaned backups (auto-restore on startup)
- `chrome_profile/` -> Chrome user data (persistent)
- `artifacts/` -> File uploads/downloads (when mounted via FastAPI)

**Data Formats:**

- **SQLite:** Primary store for all structured data
- **Parquet:** Backup export format (optional compression: zstd)
- **JSON:** Stored in `payload_json` columns for arbitrary data
- **BLOB:** Vector embeddings in `*_vec` tables

**Lifecycle:**

1. **Creation:** Via API insert or tool execution
2. **Update:** Job status transitions, log appends
3. **Retention:** Unlimited (no TTL)
4. **Deletion:** Explicit via `DELETE /api/jobs/{id}` or manual SQLite
5. **Archival:** Parquet export for cold storage

**Migration:**

No traditional schema migrations. Instead, **reconciliation**:
- On startup, `run_database_lifecycle()` calls `SchemaReconciler`
- Compares actual DB vs `expected_tables` schemas
- Missing tables created, unexpected tables dropped, drift detected
- No version numbers; schemas are source of truth

**Reset Behavior:**

- If `sumanal.db` is corrupted & `SUMANAL_ALLOW_SCHEMA_RESET=1`: Drop all, recreate from schemas
- If `logs.db` is corrupted: Same behavior (agnostic)
- Orphaned backups (`.db.bak`) are **always** restored, corrupt or not (crashes if 0 bytes)

## 8. Dependencies & Integration

**External Libraries (from `requirements.txt`):**

- `fastapi`, `uvicorn`: Web server
- `pydantic`: Data validation
- `sqlite3`: Database (built-in)
- `sqlite-vec` (optional): Vector search extension
- `pandas`, `pyarrow`: Parquet backups
- `python-telegram-bot`: Notifications
- `selenium`, `playwright`: Browser automation
- `beautifulsoup4`, `pdfminer`: Scraping/parsing
- `python-dotenv`: Config

**Why Each Exists (Evidence from Code):**
- `sqlite-vec`: Used in `connection.py` for vector tables; gracefully fails if missing
- `pandas/pyarrow`: `export_table_chunks()` in `backup/exporter.py`
- `selenium`: `utils/browser_daemon.py` for browser control
- `python-telegram-bot`: `utils/telegram/` modules for notifications
- `pydantic`: API input validation in `api/routes.py`

**Coupling Points:**

- **Tight:** API → Database writer (must be running)
- **Medium:** Worker → Tool registry (must be loaded)
- **Loose:** Browser (optional, tools degrade if unavailable)

**Environment Assumptions:**

- **OS:** Windows/Linux (path handling uses `pathlib`)
- **Python:** 3.10+
- **Disk:** Requires writable `data/` and `chrome_profile/`
- **Network:** Outbound for API calls (LLM, scraping), inbound for FastAPI
- **Chrome:** Installed and accessible via `PATH` or `CHROME_USER_DATA_DIR`

## 9. Setup, Build, and Execution

**From Clean Environment:**

```bash
# 1. Clone repository
git clone <repo> && cd AnythingTools

# 2. Create virtual environment
python -m venv .venv
# Activate: Windows: .venv\Scripts\activate; Linux: source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env with required values:
# API_KEY=your-secret-key
# SUMANAL_ALLOW_SCHEMA_RESET=1  # For dev

# 5. Ensure data directory exists
mkdir -p data chrome_profile

# 6. Run
python -m uvicorn app:app --reload --port 8000
```

**Build Processes:**

- No compilation required
- No Dockerfile provided (manual setup)
- No CI/CD scripts in repo

**Platform Constraints:**

- **SQLite:** Single-writer, so write-heavy workloads must funnel through `writer.py`
- **Chrome:** Must be installed for browser tools
- **sqlite-vec:** Optional; missing extension degrades gracefully to BLOB tables
- **Path handling:** Assumes relative to repo root; absolute paths can be set via env

## 10. Testing & Validation

**What Exists:**

- `tests/` directory exists but is minimal/empty (no functional tests observed)
- No unit tests for core components
- No integration tests

**How to Run Tests (Inferred):**

```bash
cd tests/
pytest
```

**Test Coverage Gaps (Visible from Code):**

- **Critical areas with no tests:**
  - `database/management/reconciler.py` (complex virtual table logic)
  - `bot/engine/worker.py` (polling, cancellation)
  - `api/routes.py` (API auth, validation)
  - `utils/logger/core.py` (dual logging, context vars)
  - `backup/exporter.py` (Parquet generation)
  - `startup/orchestrator.py` (tiered execution)

**Manual Validation Steps:**

1. Start app, verify startup logs show:
   - "Probing Main DB: data/sumanal.db -> MISSING"
   - "Probing Logs DB: data/logs.db -> MISSING"
   - "Validation complete" for both DBs

2. Enqueue tool, check `data/sumanal.db` and `data/logs.db` for records

3. Check `data/*.db.bak` exists after backup export

4. Corrupt `sumanal.db` (zero bytes), restart with `SUMANAL_ALLOW_SCHEMA_RESET=1`:
   - Should log "Corrupted DB detected, executing destructive reset"

## 11. Known Limitations & Non-Goals

**Hard-Coded Constraints:**

1. **Session ID Hardcoded:** All jobs use `session_id="0"` in `api/routes.py`
2. **File Paths:** Assumes `data/` and `chrome_profile/` relative to repo root
3. **Single Writer:** Each DB has one writer thread; horizontal scaling impossible
4. **No User Isolation:** Single API key, single database
5. **No Row-Level Security:** All data accessible to any valid API key caller
6. **No Job Dependencies:** Jobs cannot wait on other jobs
7. **No Retry Logic:** Failed jobs stay failed (manual cleanup required)
8. **Silent Log Drops:** Queue full drops logs silently (no retry)

**Implied but Not Implemented:**

- No WebSocket for real-time updates (client must poll `/jobs/{id}`)
- No scheduled jobs (no cron system)
- No RBAC (single API key)
- No data retention policy (no TTL on logs)

**Technical Debt:**

- `deprecated/` folder contains old code that may be imported somewhere
- `blackboard.py`, `formula_cache.py` in `database/` appear unused
- `bot/orchestrator_core/` exists but not used in current flow
- Configuration for "logger agent" mentioned in comments but removed from `config.py`

## 12. Change Sensitivity

**Most Fragile Parts:**

1. **Database Reconciliation (`database/management/reconciler.py`)**
   - **Why:** Directly executes `DROP TABLE`, `CREATE TABLE` based on schema comparison
   - **Risk:** A bug could wipe production data
   - **Mitigation:** `SUMANAL_ALLOW_SCHEMA_RESET=1` is opt-in
   - **Fragile to:** Changes in SQLite PRAGMA behavior, virtual table syntax, shadow table naming

2. **Writer Threading Model (`database/writer.py`)**
   - **Why:** Single-writer architecture with generation counters
   - **Risk:** Deadlock if multiple writers spawn, read connections not refreshed
   - **Mitigation:** Strict thread-local usage
   - **Fragile to:** Changes in `sqlite3` thread safety guarantees

3. **Startup Orchestrator (`utils/startup/orchestrator.py`)**
   - **Why:** Tiered execution with no explicit dependency declarations
   - **Risk:** Race conditions if tiers are reordered
   - **Mitigation:** Tier separation documented in code
   - **Fragile to:** Changes in concurrency, async behavior

4. **Browser Lifecycle (`utils/browser_daemon.py`)**
   - **Why:** Relies on process scanning and `selenium`/`playwright` internals
   - **Risk:** Chrome updates break zombie detection
   - **Mitigation:** Surgical kill fallback
   - **Fragile to:** OS process management changes

**Easy to Extend:**

- **New Tools:** Add module to `tools/`, it auto-discovers
- **New Schemas:** Add to `database/schemas/`, update `__init__.py` aggregator
- **New API Endpoints:** Add to `api/routes.py`
- **New Log Tags:** Use `log.dual_log(tag="MyTag", ...)`

**Hard to Modify:**

- **Dual-DB Architecture:** Requires updating `lifecycle.py`, `connection.py`, `writer.py`, `logger/core.py`
- **Virtual Table Logic:** Changes to `reconciler.py` must match SQLite internals
- **Startup Tiers:** Reordering requires knowledge of all dependencies