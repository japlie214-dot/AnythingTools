# AnythingTools

## 1. Project Overview
AnythingTools is a durable, tool-augmented background service designed for high-reliability web scraping, financial data extraction, and automated publishing to Telegram. It solves the fragility of LLM-driven browser automation by decoupling request submission from execution, implementing strict state persistence, utilizing Semantic Object Models (SoM) for DOM interaction, and providing a robust crash-recovery framework.

### Operational Purpose
The system provides a managed, asynchronous execution environment for long-running browser tasks. It ensures progress is never lost during network failures, DOM changes, or process crashes by tracking granular job items in a SQLite database. It provides a Human-in-the-Loop (HITL) mechanism to pause and resume tasks, and a robust 2-tier synchronization engine for disaster recovery and analytical syncing between the Operational DB and a cloud Snowflake warehouse.

### Explicit Non-Goals
- **Not a Chatbot**: It is an asynchronous job processor, not a real-time interactive chat interface.
- **No Frontend**: It exposes a REST API for external orchestration (e.g., AnythingLLM).
- **No Internal LLM**: It integrates with external providers (Azure OpenAI, Chutes) via a client abstraction layer.

## 2. High-Level Architecture
The system implements a **Producer-Consumer** pattern centered around a SQLite-backed job queue with strict single-writer database constraints.

### Major Components
- **API (`api/`)**: FastAPI interface for job enqueueing, status polling, resumption, and system observability.
- **Unified Worker Manager (`bot/engine/worker.py`)**: A singleton daemon that polls the `jobs` table and spawns isolated threads for tool execution.
- **Tool Registry (`tools/registry.py`)**: A dynamic discovery system that instantiates `BaseTool` subclasses.
- **Orchestrator (`bot/orchestrator_core/`)**: Middleware that enhances browser interaction by injecting a Semantic Object Model (SoM) into the DOM before LLM evaluation.
- **Database Layer (`database/`)**: A multi-database architecture (`sumanal.db`, `logs.db`) utilizing dedicated single-writer threads to eliminate SQLite locking contention.
- **Sync Subsystem (`database/backup/`)**: A 2-tier synchronization engine (`SyncEngine`) maintaining parity between the Operational DB and a cloud Snowflake warehouse. It utilizes an asynchronous `cloud_writer` for real-time, best-effort updates.

### Data Flow
1. **Submission**: `POST /api/tools/{tool_name}` $\rightarrow$ API validates input via Pydantic $\rightarrow$ Job inserted into `jobs` table as `QUEUED`.
2. **Dispatch**: `UnifiedWorkerManager` polls `jobs` $\rightarrow$ Spawns execution thread $\rightarrow$ Sets status to `RUNNING`.
3. **Execution**: `Worker` $\rightarrow$ `ToolRegistry` (instantiation) $\rightarrow$ `Tool.run()` (invokes LLMs, bots, and SoM injection).
4. **Persistence**: Tool results are written to `jobs.result_json` and detailed progress is tracked in `job_items`. All writes queue through `database.writer`.
5. **Inline Cloud Sync**: Mutating operations in the application layer (e.g., `ArticleStore`, `BroadcastWriter`) trigger `enqueue_cloud_write`, which pushes data to Snowflake asynchronously via a background queue.
6. **Completion**: `_do_callback_with_logging` sends the final result to the calling system via HTTP POST.
7. **Lifecycle Sync**: On startup, `SyncEngine` pulls from Snowflake to synchronize the local operational state. On shutdown, the system drains the `cloud_write_queue` and performs a final delta sync to Snowflake.

## 3. Repository Structure
- `api/`: REST endpoints (`routes.py`) and Pydantic validation schemas (`schemas.py`).
- `bot/`:
    - `engine/`: The core polling loop (`worker.py`) and safety wrappers (`tool_runner.py`).
    - `orchestrator_core/`: Logic for SoM markers and context budget eviction.
- `clients/`: External integrations (LLM clients for Azure/Chutes, Snowflake client for native embedding generation).
- `database/`: 
    - `connection.py`, `writer.py`, `logs_writer.py`: Thread-safe database managers.
    - `schemas/`: Canonical SQL definitions.
    - `backup/`: The SyncEngine system. Contains `engine/` (`SyncEngine`, `CloudEngine`), `sync/` (`DiffEngine`, `resolution`, `smart_recommender`), and `writer/` (`cloud_writer.py` for async Snowflake writes).
    - `broadcast/`: Domain logic for Telegram publishing state.
    - `management/`: Schema reconciliation and database health checks.
- `tools/`:
    - `base.py`: Abstract base class and `ResumeReport` contracts.
    - `registry.py`: Whitelist-based dynamic tool discovery.
    - `scraper/`: Browser-based extraction, hitl escalation, and LLM curation.
    - `publisher/`: Telegram delivery pipeline with sliding-window rate limiting.
    - `draft_editor/`: Atomic manipulation of curated lists.
    - `stock_notes/`: SEC EDGAR footnote extraction and tidy-format financial data storage.
    - `batch_reader/`: Hybrid semantic + keyword search across batches.
- `utils/`: Cross-cutting utilities (logging, artifact management, browser daemon, SoM Javascript injection, rate limiters, text sanitization).
- `scripts/`: Maintenance utilities.
- `deprecated/`: Legacy logic (e.g., `bot/core/agent.py`, `tools/finance/`) that has been superseded by the current modular tool architecture.

## 4. Core Concepts & Domain Model
### Key Abstractions
- **Job**: The primary unit of work tracked by a ULID. States: `QUEUED`, `RUNNING`, `PAUSED_FOR_HITL`, `COMPLETED`, `FAILED`, `INTERRUPTED`, `CANCELLING`.
- **SoM (Semantic Object Model)**: Injection of `data-ai-id` attributes into the DOM (via JS) to provide the LLM with deterministic element references, bypassing fragile CSS selectors.
- **Resume Mechanism**: Tool-specific logic (`ResumeHandler`) that queries domain tables (e.g., `job_items`) to determine the exact point of resumption after a crash or HITL pause.
- **WriteReceipt**: A synchronization primitive that blocks synchronous code until an asynchronous database write is committed by the writer thread.
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
5. The tool returns a result; Worker updates the DB and triggers the HTTP callback.

### Failure Modes & Error Handling
- **Crash Recovery**: If the application crashes, the startup sequence (`utils/startup/recovery.py`) downgrades `RUNNING` jobs to `INTERRUPTED`. The `UnifiedWorkerManager` automatically retries these.
- **HITL Pause**: Tools can raise a `PAUSED_FOR_HITL` signal (e.g., encountering a paywall). Execution halts until a POST to `/resume` is received.
- **Doom Loop Prevention**: The `/resume` endpoint increments `resume_count`. If it exceeds `MAX_RESUME_ATTEMPTS`, the job is poisoned and marked `FAILED`.
- **Sync Conflicts (Split-Brain)**: If versions of a row differ between Operational and Cloud states, `ConflictResolver` flags it. The system employs automated strategies (`newest_overall_wins`, etc.) or escalates to the `UserConfirmationHandler` for manual terminal-based decision.
- **Circuit Breaking**: If Snowflake is unreachable, `CircuitBreaker` opens, and CloudEngine operations fail fast. The system operates locally and sets a `sync_pending` flag for future reconciliation.

## 6. Public Interfaces
### REST API
- `POST /api/tools/{tool_name}`: Enqueues a tool execution. Requires valid payload matching the tool's `INPUT_MODEL`.
- `GET /api/jobs/{job_id}`: Returns status, logs, and final payload.
- `DELETE /api/jobs/{job_id}`: Marks a job as `CANCELLING` to trigger graceful termination.
- `POST /api/jobs/{job_id}/resume`: Resumes a paused or interrupted job.
- `GET /api/backup/status`: Returns `BackupMetricsResponse` containing health, sync state, and circuit breaker status for the SyncEngine.
- `GET /api/manifest`: Returns available tools and their JSON schemas for LLM orchestration.

### Internal Tool Registry
- `REGISTRY.create_tool_instance(name)`: Returns a fresh tool instance.
- `REGISTRY.schema_list()`: Returns MCP-compatible tool definitions.

## 7. State, Persistence, and Data
### Storage Locations
- **Operational DB (`data/sumanal.db`)**: Source of truth for `jobs`, `job_items`, `scraped_articles`, `broadcast_batches`, `sn_filings`, `sn_notes`, `sn_detail_registry`, `sn_note_details`, and the `sync_ledger`.
- **Telemetry DB (`data/logs.db`)**: High-throughput event store. Recreated fresh on every application startup.
- **Snowflake**: Cloud target for analytical querying and remote backup.
- **Artifacts (`data/temp/multimodal` / `artifacts/`)**: Ephemeral or receipt files (JSON, screenshots) served via the API.

### Data Management Rules
- Schema management is performed at startup via `database.management.reconciler`.
- Local schema reconciliation uses surgical `ALTER TABLE` operations to add or drop columns. Column drops are protected by pre-drop backups via the SQLite native `.backup()` API and automated pruning of dependent indexes.
- **Operational Migration Pipeline**: For critical type or constraint drift, the system implements a schema-driven migration pipeline: `Clone` $\rightarrow$ `Recreate` $\rightarrow$ `Repopulate` $\rightarrow$ `Autofill` $\rightarrow$ `Validate`. This ensures data is preserved and validated before any destructive schema changes are finalized.
- Cloud schema management (`SnowflakeSchemaManager`) supports bidirectional evolution, performing both `ADD COLUMN` and `DROP COLUMN` operations to maintain parity with the Operational DB.
- SQLite DDL is transpiled to Snowflake DDL at runtime using `sqlglot`. Embedding fields (`float[1024]`) are dynamically mapped to Snowflake native `VECTOR(FLOAT, 1024)`.
- **Cloud Rebuild Pipeline**: When Snowflake type drift is detected, the system performs a full table rebuild using the operational DB as the source of truth. This utilizes a temporary staging table and `INSERT INTO ... SELECT` syntax to bypass Snowflake `VALUES` clause compilation limits for `VECTOR` types.
- **Constraint Handling**: The system strips `DEFAULT CURRENT_TIMESTAMP` from Snowflake DDL to avoid type mismatches between `VARCHAR` and `TIMESTAMP_LTZ`. To maintain integrity, timestamps are generated in Python and explicitly inserted.

## 8. Dependencies & Integration
- **FastAPI**: REST API layer.
- **SQLite (stdlib)**: Primary persistence mechanism.
- **sqlite-vec**: C extension for vector similarity search (`MATCH`).
- **Snowflake-SQLAlchemy & Cryptography**: Connection pooling, key-pair authentication, and `INSERT` operations for cloud sync.
- **sqlglot**: SQL dialect transpilation (SQLite -> Snowflake).
- **Pydantic & Pydantic-Settings**: Data validation, input schema generation, and strict environment configuration (`BackupSettings`).
- **Botasaurus**: Chrome automation (stealth, CDP interactions).
- **python-telegram-bot**: Passive delivery of translated briefings.
- **OpenAI SDK**: Interface to Azure OpenAI and Chutes (Llama 3) models.

## 9. Setup, Build, and Execution
1. Install dependencies: `pip install -r requirements.txt`. (Requires compiling `sqlite-vec` if not using pre-built wheels).
2. Configure `.env` file (parsed by `pydantic-settings` for backup) and environment variables for legacy configs (`config.py`).
    - Required: `API_KEY`, `AZURE_ENDPOINT`, `EDGAR_IDENTITY`.
    - Optional Backup: `BACKUP_CLOUD__ACCOUNT`, `BACKUP_CLOUD__USER`, etc.
3. Run the application: `python -m uvicorn app:app --reload --port 8000`.
4. *Startup Sequence*: The app enforces `WEB_CONCURRENCY=1`, mounts static artifacts, drops and recreates `logs.db`, validates `sqlite-vec`, runs schema migrations, initializes the `SyncEngine` and `cloud_writer`, executes a cloud pull sync, and warms up the browser daemon.

## 10. Testing & Validation
- **Health Checks**: Extensive runtime health checks during startup (e.g., PRAGMA `integrity_check`, CDP ping probes in `ChromeDaemonManager`).
- **Diagnostics API**: `/api/diagnostics` and `/api/metrics` expose queue depths, dropped logs, and active job counts.
- **Unit/Integration Testing**: Test suites exist for E2E browser flows and backup mechanics.
- **Gaps**: Isolated unit test coverage is low for individual agent actions; heavily reliant on E2E flows and manual log reconstruction.

## 11. Known Limitations & Non-Goals
- **Single-Node Limitation**: The architecture strictly relies on in-memory locks (`browser_lock.py`), in-memory context variables (`utils/logger/state.py`), and a local SQLite WAL. It cannot be horizontally scaled across multiple containers.
- **Browser State Fragility**: Relying on headless/headful Chrome introduces inherent fragility regarding CDP timeouts, zombie processes, and changing target DOMs.
- **Telegram Limits**: Global API rate limits are heavily constrained; `SlidingWindowRateLimiter` ensures compliance but forces slow, serialized delivery of large news batches.
- **Memory Consumption**: `DiffEngine` uses SQLite temp tables to avoid Python OOMs, but massive data transfers in Snowflake may still incur noticeable memory overhead in SQLAlchemy.

## 12. Change Sensitivity
- **Extremely Fragile**: `database/writer.py` and `database/logs_writer.py`. Altering the queue logic, thread handling, and transaction boundaries here will cause immediate database locking or silent data loss.
- **Type-Sensitive**: `database/backup/engine/cloud_engine.py` and `database/backup/writer/cloud_writer.py`. The interaction between the Snowflake Python connector's query optimizer and the native `VECTOR` type is highly specific; changing the `INSERT ... SELECT` or staging table pattern will likely trigger `InterfaceError` or compilation failures.
- **Tightly Coupled**: The `SyncEngine` synchronization (`database/backup/engine/`) heavily relies on `database/backup/schema_registry.py` for precise type mapping. Changing SQLite schemas requires verifying the `sqlglot` output for Snowflake.
- **Schema Evolution**: The `database/management/reconciler` is tightly coupled with `database/schemas/column_defaults.py` for auto-filling computed columns (e.g., `content_hash`) after surgical additions.
- **Easily Extensible**: Adding a new tool is trivial. Create a subclass of `BaseTool` in `tools/`, define an `INPUT_MODEL`, and it will be automatically discovered by `registry.py` and exposed via the `/api/manifest` endpoint.