# AnythingTools

## 1. Project Overview
AnythingTools is a durable, tool-augmented background service designed for high-reliability web scraping, financial data extraction, and automated publishing to Telegram. It solves the fragility of LLM-driven browser automation by decoupling request submission from execution, implementing strict state persistence, utilizing Semantic Object Models (SoM) for DOM interaction, and providing a robust crash-recovery framework.

### Operational Purpose
The system provides a managed, asynchronous execution environment for long-running browser tasks. It ensures progress is never lost during network failures, DOM changes, or process crashes by tracking granular job items in a SQLite database. It provides a Human-in-the-Loop (HITL) mechanism to pause and resume tasks via a REST API and a robust 2-tier synchronization engine for disaster recovery and analytical syncing between the Operational DB and a cloud Snowflake warehouse.

### Explicit Non-Goals
- **Not a Chatbot**: It is an asynchronous job processor, not a real-time interactive chat interface.
- **No Frontend**: It exposes a REST API for external orchestration (e.g., AnythingLLM).
- **No Internal LLM**: It integrates with external providers (Azure OpenAI, Chutes) via a client abstraction layer.

## 2. High-Level Architecture
The system implements a **Producer-Consumer** pattern centered around a SQLite-backed job queue with strict single-writer database constraints.

### Major Components
- **API (`api/`)**: FastAPI interface for job enqueueing, status polling, SSE event streaming, resumption, and system observability. Supports `capture_lineage` requests to retrieve detailed tool execution traces.
- **Unified Worker Manager (`bot/engine/worker.py`)**: A singleton daemon that polls the `jobs` table and spawns isolated threads for tool execution.
- **Tool Registry (`tools/registry.py`)**: A dynamic discovery system that instantiates `BaseTool` subclasses.
- **Orchestrator (`bot/orchestrator_core/`)**: Middleware that enhances browser interaction by injecting a Semantic Object Model (SoM) into the DOM before LLM evaluation.
- **Database Layer (`database/`)**: A multi-database architecture (`sumanal.db`, `logs.db`) utilizing dedicated single-writer threads to eliminate SQLite locking contention.
- **Sync Subsystem (`database/backup/`)**: A 2-tier synchronization engine (`SyncEngine`) maintaining parity between the Operational DB and a cloud Snowflake warehouse. It utilizes an asynchronous `cloud_writer` for real-time, best-effort updates and a robust `CloudEngine` for scheduled reconciliation.
- **SSE Subsystem (`api/sse/`)**: A real-time event streaming layer that projects job execution logs and phase transitions (started, running, paused, completed) to clients.

### Data Flow
1. **Submission**: `POST /api/tools/{tool_name}` $\rightarrow$ API validates input via Pydantic $\rightarrow$ Job inserted into `jobs` table as `QUEUED`.
2. **Dispatch**: `UnifiedWorkerManager` polls `jobs` $\rightarrow$ Spawns execution thread $\rightarrow$ Sets status to `RUNNING`.
3. **Execution**: `Worker` $\rightarrow$ `ToolRegistry` (instantiation) $\rightarrow$ `Tool.run()` (invokes LLMs, bots, and SoM injection).
4. **Persistence**: Tool results are written to `jobs.result_json` and detailed progress is tracked in `job_items`. All writes queue through `database.writer`.
5. **Inline Cloud Sync**: Mutating operations in the application layer trigger `enqueue_cloud_write`, which pushes data to Snowflake asynchronously.
6. **Streaming**: Clients connect to `GET /api/jobs/{job_id}/stream`. The `SseProjector` tails `logs.db`, deriving phases from `status_state` and yielding WHATWG-compliant events.
7. **HITL Pause**: If a tool raises `HitlPaused`, the worker transitions the job to `PAUSED_FOR_HITL` and blocks the thread on a `threading.Event` in the `HitlResolutionRegistry`.
8. **Resumption**: `POST /api/jobs/{job_id}/resume` provides a decision ("proceed", "skip", "cancel"), which unblocks the worker thread.
9. **Lifecycle Sync**: On startup, `SyncEngine` pulls from Snowflake to synchronize local state. On shutdown, the system signals SSE projectors to close and drains the `cloud_write_queue`.

## 3. Repository Structure
- `api/`: REST endpoints (`routes.py`) and Pydantic validation schemas (`schemas.py`).
    - `sse/`: SSE streaming logic including `projector.py` (async generator), `envelope.py` (wire-format), `phases.py` (state mapping), and `log_notify.py` (async wakeup bus).
- `bot/`:
    - `engine/`: The core polling loop (`worker.py`) and safety wrappers (`tool_runner.py`). Manages the lifecycle of `ActivityAccumulator` for observability.
    - `orchestrator_core/`: Logic for SoM markers and context budget eviction.
- `clients/`: External integrations (LLM clients for Azure/Chutes, Snowflake client for native embedding generation).
- `database/`: 
    - `connection.py`, `writer.py`, `logs_writer.py`: Thread-safe database managers.
    - `schemas/`: Canonical SQL definitions, including `_snowflake_overrides.py` for type-mapping exceptions.
    - `backup/`: The SyncEngine system. Contains `engine/` (`SyncEngine`, `CloudEngine`, `SnowflakeSchemaManager`), `resilience/` (`CircuitBreaker`, `session_recovery.py`), `sync/` (`DiffEngine`, `resolution`, `smart_recommender`), and `writer/` (`cloud_writer.py` for async Snowflake writes).
    - `broadcast/`: Domain logic for Telegram publishing state.
    - `management/`: Schema reconciliation, migration coordination, and database health checks.
    - `sse_retire_pending_callback.py`: Startup utility to migrate legacy `PENDING_CALLBACK` rows.
- `tools/`:
    - `base.py`: Abstract base class, `ResumeReport` contracts, and `HitlPaused` exception.
    - `registry.py`: Whitelist-based dynamic tool discovery.
    - `scraper/`: Browser-based extraction, hitl escalation, and LLM curation.
    - `publisher/`: Telegram delivery pipeline with sliding-window rate limiting.
    - `draft_editor/`: Atomic manipulation of curated lists.
    - `stock_financials/`: SEC EDGAR quarterly fact extraction and tabular storage.
    - `stock_notes/`: SEC EDGAR footnote extraction, tidy-format transformation, and concept cataloging.
    - `batch_reader/`: Hybrid semantic + keyword search across batches.
- `utils/`: Cross-cutting utilities (logging, artifact management, browser daemon, SoM Javascript injection, rate limiters, text sanitization).
    - `observability/`: Activity-driven observability framework providing the `@activity` decorator and `LineageReport` generation.
    - `hitl_resolution.py`: Process-wide registry for resolving HITL pauses via API.
    - `sse_health/`: Embedded health checkers for SSE functionality.
- `scripts/`: Operational utilities, including `logs_query.py` for read-only inspection of the logs database.
- `deprecated/`: Legacy logic (e.g., `tools/finance/`) that has been superseded by the current modular tool architecture.

## 4. Core Concepts & Domain Model
### Key Abstractions
- **Job**: The primary unit of work tracked by a ULID. States: `QUEUED`, `RUNNING`, `PAUSED_FOR_HITL`, `COMPLETED`, `FAILED`, `INTERRUPTED`, `CANCELLING`.
- **SoM (Semantic Object Model)**: Injection of `data-ai-id` attributes into the DOM (via JS) to provide the LLM with deterministic element references, bypassing fragile CSS selectors.
- **Resume Mechanism**: Tool-specific logic (`ResumeHandler`) that queries domain tables (e.g., `job_items`) to determine the exact point of resumption after a crash or HITL pause.
- **SseProjector**: An async generator that maintains a dedicated read-only SQLite connection to `logs.db` and projects events based on `event_id` monotonicity.
- **LineageReport**: A detailed execution trace comprising ordered `ActivityRecord`s, providing a deterministic audit of tool internal logic for a specific job.
- **2-Tier Sync**: A synchronization model that treats the Operational DB as the source of truth and Snowflake as the durable cloud mirror. It uses a `sync_ledger` for watermarking and a 2-way `DiffEngine` for conflict detection.

### Invariants
- **Single Writer**: All writes to operational databases MUST pass through the `database.writer` or `database.logs_writer` queues.
- **Read-Only Connections**: Direct queries (`DatabaseManager.get_read_connection()`) enforce `PRAGMA query_only = ON`.
- **Browser Lock**: Only one browser-based tool can execute at a time, enforced by `utils/browser_lock.py` (`BrowserLockProxy`).
- **Artifacts as Receipts**: Files in the `artifacts/` directory are for audit/debug only. Operational state is strictly derived from the SQLite database.
- **Single Process**: The application enforces `WEB_CONCURRENCY=1` at startup to prevent state corruption.

## 5. Detailed Behavior
### Normal Execution
1. API enqueues a job.
2. Worker picks up the job, marks it `RUNNING`, and instantiates the tool.
3. Tool executes. If browser-based, the Botasaurus driver navigates, and the Orchestrator injects SoM markers via `run_js`.
4. The tool streams progress into `job_items`.
5. The tool returns a result; Worker updates the DB.
6. SSE clients receive the `completed` event via the projector, which decodes structured tool results into a final payload.

### Failure Modes & Error Handling
- **Crash Recovery**: If the application crashes, the startup sequence (`utils/startup/recovery.py`) downgrades `RUNNING` jobs to `INTERRUPTED`. The `UnifiedWorkerManager` automatically retries these.
- **HITL Pause**: Tools raise a `HitlPaused` exception. The worker transitions the job to `PAUSED_FOR_HITL` and blocks the thread. Execution resumes only after a valid `POST /resume` decision is delivered via the `HitlResolutionRegistry`.
- **Doom Loop Prevention**: The `/resume` endpoint increments `resume_count`. If it exceeds `MAX_RESUME_ATTEMPTS`, the job is marked `FAILED`. Note: HITL resumes bypass this count.
- **Sync Conflicts (Split-Brain)**: If versions of a row differ between Operational and Cloud states, `ConflictResolver` flags it. The system employs automated strategies or escalates to the `UserConfirmationHandler`.
- **Cloud Session Recovery**: If Snowflake expires a session, the `CloudEngine` employs a `handle_error` listener to invalidate the connection pool and a `with_session_recovery` decorator to retry the operation.
- **Circuit Breaking**: If Snowflake is unreachable, `CircuitBreaker` opens and CloudEngine operations fail fast.

### Financial Data Extraction Workflow (SEC EDGAR)
The `stock_financials` and `stock_notes` tools implement a multi-stage pipeline:
1. **Discovery**: Identify relevant filings (10-K, 10-Q) via SEC EDGAR.
2. **Extraction**: 
    - `stock_financials`: Extracts quarterly facts into `sf_quarterly_facts`.
    - `stock_notes`: Extracts full filing text, identifies footnotes, and decomposes them into "tidy" detail tables.
3. **Transformation**: `stock_notes` uses a `tidy_transform` process to convert complex XBRL/HTML tables into a normalized `sn_note_details` format.
4. **Querying**: Provides a "Concept Catalog" allowing users to query specific financial concepts (e.g., `us-gaap:Assets`) across time series.

## 6. Public Interfaces
### REST API
- `POST /api/tools/{tool_name}`: Enqueues a tool execution. Requires valid payload matching the tool's `INPUT_MODEL`.
- `GET /api/jobs/{job_id}`: Returns status, logs, and final payload.
- `GET /api/jobs/{job_id}/stream`: SSE stream for real-time execution events. Supports `Last-Event-ID` for reconnection.
- `DELETE /api/jobs/{job_id}`: Marks a job as `CANCELLING` to trigger graceful termination.
- `POST /api/jobs/{job_id}/resume`: Resumes a paused or interrupted job. For HITL jobs, accepts a `decision` ("proceed", "skip", "cancel").
- `GET /api/backup/status`: Returns `BackupMetricsResponse` containing health, sync state, and circuit breaker status for the SyncEngine.
- `GET /api/manifest`: Returns available tools and their JSON schemas for LLM orchestration.

### Logs Query CLI (`scripts/logs_query.py`)
A standalone, read-only utility for inspecting `logs.db`.
- **Input**: Command line arguments (`recent`, `errors`, `by-tag`, `by-job`, `search`, `show`, `stats`, `tags`, `tail`).
- **Path Resolution**: Resolves `logs.db` via `--db` arg $\rightarrow$ `LOGS_DB_PATH` env $\rightarrow$ `OPERATIONAL_DB_PATH` parent $\rightarrow$ `./data/logs.db`.
- **Output**: Markdown tables/sections by default; JSON via `--json`.
- **Constraints**: Opens database in read-only URI mode (`mode=ro`) to prevent write side-effects.

### Internal Tool Registry
- `REGISTRY.create_tool_instance(name)`: Returns a fresh tool instance.
- `REGISTRY.schema_list()`: Returns MCP-compatible tool definitions.

## 7. State, Persistence, and Data
### Operational Storage (SQLite)
- **`jobs` / `job_items`**: Core task tracking and granular progress logs.
- **`sf_tickers` / `sf_quarterly_facts`**: Cached financial data. `sf_quarterly_facts` uses a composite PK `(ticker, statement_type, concept, quarter)`.
- **`sn_filings` / `sn_notes` / `sn_note_details`**: SEC filing hierarchy. `sn_note_details` stores normalized footnote data.
- **`dead_letter_queue`**: Stores failed cloud writes for manual recovery.
- **`logs.db`**: Structured system logs with `timestamp`, `level`, `tag`, `job_id`, and `payload_json`.

### Cloud Storage (Snowflake)
- **Mirroring**: Every persisted SQLite table is mirrored in Snowflake.
- **Vector Storage**: Tables containing `embedding` columns are pushed using the `VectorSync` engine, mapping SQLite BLOBs to Snowflake `VECTOR(FLOAT, 1024)`.
- **Composite Keys**: The `CloudEngine` supports both single-column (`id`) and composite PKs to generate `MERGE` statements in Snowflake, ensuring idempotency.
- **Type Overrides**: A registry in `database/schemas/_snowflake_overrides.py` allows specific columns to bypass generic transpilation to prevent spurious table rebuilds.

## 8. Dependencies & Integration
- **LLM Providers**: Azure OpenAI and Chutes (via `clients/llm/`).
- **Browser Automation**: Botasaurus / Chrome (via `utils/browser_daemon.py`).
- **Database**: SQLite (Operational) and Snowflake (Backup/Analytics).
- **Frameworks**: FastAPI (API), Pydantic (Validation), SQLAlchemy (Cloud Connectivity), Pandas (Data Processing).
- **Critical Versions**: `snowflake-connector-python>=3.7.0` (for session token support), `snowflake-sqlalchemy>=1.6.0`, and `fastapi>=0.135.0` (for native SSE support).

## 9. Setup, Build, and Execution
### Environment Configuration
Requires a `.env` file containing:
- `SNOWFLAKE_*`: Credentials for cloud backup.
- `AZURE_OPENAI_*` / `CHUTES_*`: LLM API keys.
- `EDGAR_IDENTITY`: Required for SEC EDGAR access.

### Execution
```bash
# Install dependencies
pip install -r requirements.txt

# Start the service
python -m uvicorn app:app --port 8000
```
*Note: Must be run with `WEB_CONCURRENCY=1` to prevent database corruption.*

## 10. Testing & Validation
- **`tests/test_backup.py`**: Validates the `SyncEngine`'s ability to detect and resolve drifts, composite PK detection, and session recovery logic.
- **`tests/test_inspect_notes.py`**: Live SEC EDGAR contract test for the edgartools API surface.
- **`tests/test_browser_e2e.py`**: E2E validation of the scraper tool's browser-orchestrator loop.
- **`utils/sse_health/check_sse_stream.py`**: Embedded health checker that exercises the full SSE flow (Happy Path, Reconnect, Terminal 409s) against the staging DB.

## 11. Known Limitations & Non-Goals
- **SQLite Locking**: While the single-writer pattern mitigates locking, extremely high write volumes may still cause contention.
- **Browser Overhead**: Chrome instances are resource-intensive; the system limits execution to one browser-tool at a time.
- **Cloud Latency**: Real-time Snowflake writes are "best-effort"; the `SyncEngine` is the final authority for data consistency.
- **Parameter Limits**: SQLite host parameter limits (999) constrain the size of batch operations for composite PK tables.

## 12. Change Sensitivity
- **Schema Changes**: Any change to `database/schemas/` requires a corresponding migration. The `SchemaReconciler` will detect mismatches and may trigger a table recreation.
- **Tool Interface**: Modifying `INPUT_MODEL` in a tool changes the API contract and the `manifest` exposed to the LLM.
- **Snowflake DDL**: Changes to the cloud schema must be managed via `BackupSchemaRegistry` and the override registry to ensure `sqlglot` transpilation remains consistent.