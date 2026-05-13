# AnythingTools

## 1. Project Overview
AnythingTools is a high-reliability tool execution system designed to bridge LLM-driven orchestration with durable, stateful tool execution. It provides a robust framework for running long-running, browser-based, and data-intensive tasks (such as web scraping, curation, and financial data extraction) while ensuring execution continuity, crash recovery, and strict observability.

The system solves the "transient execution" problem by treating every tool call as a durable `Job` persisted in a database, allowing for interrupted jobs to be resumed and for every state transition to be audited via a dual-logging system.

**What it explicitly does NOT do:**
- It is not a general-purpose chatbot; it is an execution engine for tools.
- It does not manage LLM prompt engineering for the primary agent; it provides the *infrastructure* (context, tool definitions, and execution) that an external agent (like AnythingLLM) uses.

## 2. High-Level Architecture
The system follows a **Producer-Consumer** architecture with a **Durable Ledger** as the synchronization point.

### Data Flow
1. **Ingress**: A request arrives via the FastAPI `api/routes.py`.
2. **Job Persistence**: The request is validated and persisted as a `QUEUED` job in the `jobs` table of the SQLite database.
3. **Polling & Execution**: The `UnifiedWorkerManager` (`bot/engine/worker.py`) polls the database for `QUEUED` or `INTERRUPTED` jobs.
4. **Safe Execution**: The worker spawns a thread and uses `run_tool_safely` (`bot/engine/tool_runner.py`) to invoke the tool.
5. **Orchestration (SoM)**: For browser-bound tools, the `OrchestratorRouter` (`bot/orchestrator_core/router.py`) injects Semantic Object Model (SoM) markers into the DOM to provide the LLM with precise element targeting.
6. **Callback**: Upon completion, the worker executes an HTTP callback to the orchestrating LLM (e.g., AnythingLLM) with a structured markdown summary of the result.
7. **Persistence**: Final results and logs are persisted via the `database/writer.py` background thread.

### Control Flow & Lifecycle
- **Startup**: Managed by `utils/startup`, involving zombie-chrome cleanup, DB migration, tool registry loading, and a critical delta reconciliation of the article store.
- **Runtime**: Asynchronous API handling combined with synchronous, threaded tool execution.
- **Shutdown**: A phased drain sequence that stops the worker polling, cancels active jobs, and flushes the DB write queue.

## 3. Repository Structure
- `api/`: FastAPI routes and schemas. The entry point for all external tool triggers.
- `bot/`:
    - `engine/`: The core execution logic. `worker.py` manages the job lifecycle; `tool_runner.py` handles safe execution.
    - `orchestrator_core/`: Logic for SoM (Semantic Object Model) context building and element targeting.
- `clients/`: External service integrations.
    - `llm/`: Unified interface for LLM providers (Azure, Chutes), including request/response types.
- `database/`: Persistence layer.
    - `connection.py`: Thread-local SQLite connection management.
    - `writer.py`: A dedicated background thread for all writes to prevent database locks.
    - `articles/`: Mutable storage engine (`store.py`), reconciliation logic (`reconcile.py`), and data models.
    - `backup/`: Configuration and tools for system-wide backups.
- `tools/`: The tool library.
    - `registry.py`: Dynamic discovery and instantiation of `BaseTool` subclasses.
    - `scraper/`: Complex pipeline for extracting and curating web content.
- `utils/`: Shared utilities.
    - `logger/`: A "dual-logger" system that writes to both standard logs and a durable SQLite `logs.db`.
    - `browser_daemon.py`: Manages a persistent headless Chrome instance.
- `deprecated/`: Historical versions of agents and tools, serving as evidence of architectural evolution.

## 4. Core Concepts & Domain Model
### The Job Ledger
The `jobs` table is the single source of truth. A job's state transitions are:
`QUEUED` $\rightarrow$ `RUNNING` $\rightarrow$ (`COMPLETED` | `FAILED` | `PARTIAL` | `ABANDONED` | `PAUSED_FOR_HITL`).

### SoM (Semantic Object Model)
To solve the fragility of CSS selectors, the system injects `data-ai-id` attributes into the DOM. The `OrchestratorRouter` ensures the LLM knows the range of available markers, allowing it to reference elements by ID rather than fragile paths.

### Write-Ahead-Log (WAL) & Generation Tracking
Because the system uses a single-writer model, `database/writer.py` increments a `_write_generation` counter. Read connections in `DatabaseManager` monitor this generation to force a connection refresh when new data is committed, ensuring read-after-write consistency.

### Article Manifest System
The article pipeline uses a mutable JSON/BIN manifest system. `manifest.json` tracks the authoritative state of scraped articles, while individual `.json` (metadata) and `.bin` (embeddings) files ensure per-article atomic I/O.

## 5. Detailed Behavior
### Normal Execution
1. API receives `/tools/scraper` $\rightarrow$ Job created in DB.
2. Worker picks up job $\rightarrow$ Spawns thread $\rightarrow$ Instantiates `ScraperTool`.
3. `ScraperTool` runs a pipeline: Extraction $\rightarrow$ Slimming $\rightarrow$ Curation $\rightarrow$ Artifact Generation.
4. Curation uses a 3-retry loop with a "Budget" (knapsack-style) to fit as many candidates as possible into the LLM context.
5. Worker sends callback to AnythingLLM $\rightarrow$ Job marked `COMPLETED`.

### Failure Modes & Resilience
- **Crash Recovery**: If the process dies, jobs remain as `RUNNING`. On restart, the `UnifiedWorkerManager` identifies these as `INTERRUPTED` and allows them to be resumed.
- **HITL (Human-In-The-Loop)**: Tools can raise a `PAUSED_FOR_HITL` exception, stopping execution and updating the job status until a manual resume is triggered via API.
- **Database Poisoning**: If the writer thread encounters 3 consecutive errors, it assumes the connection is poisoned and forces a reconnection.
- **Article Store Self-Healing**: During startup, the `reconcile_delta` process identifies "ghost" entries (manifest entries without corresponding files) and purges them.

## 6. Public Interfaces
### REST API
- `POST /api/tools/{tool_name}`: Enqueues a tool execution. Requires `X-API-Key`.
- `GET /api/jobs/{job_id}`: Returns status, logs, and final payload.
- `DELETE /api/jobs/{job_id}`: Requests cancellation.
- `POST /api/jobs/{job_id}/resume`: Resumes an interrupted/failed job.
- `GET /api/manifest`: Returns the list of registered tools and their JSON schemas.

### Tool Registry
Tools must subclass `BaseTool` and be located in `tools/`. They can optionally define an `INPUT_MODEL` (Pydantic) for automatic API validation.

## 7. State, Persistence, and Data
- **Primary DB (`sumanal.db`)**: Stores jobs, job items, and scraped articles.
- **Logs DB (`logs.db`)**: High-throughput storage for all system events.
- **Vector Storage**: Uses the `sqlite-vec` extension for float32 vector embeddings.
- **Article Store**: 
    - `manifest.json`: authoritative state and checksums.
    - `articles/*.json`: Per-article metadata.
    - `articles/*.bin`: Binary embedding data.
- **Artifacts**: JSON and PDF files are stored in the `artifacts/` directory, mapped to job IDs.

## 8. Dependencies & Integration
- **FastAPI/Uvicorn**: Web layer.
- **Botasaurus**: Browser automation and scraping.
- **OpenAI/Azure**: LLM providers for curation and tool orchestration.
- **SQLite (with sqlite-vec)**: Durable state and vector search.
- **PyArrow**: Used in backup systems for table exports.
- **AnythingLLM**: The primary external consumer of the tool results via HTTP callbacks.

## 9. Setup, Build, and Execution
1. **Environment**: Python 3.10+
2. **Binary Requirement**: Install `sqlite-vec` extension for vector search.
3. **Configuration**: Set variables in `config.py` (API keys, DB paths).
4. **Run**: `python -m uvicorn app:app --reload --port 8000`
5. **Concurrency Constraint**: Must be run with `workers=1` (enforced in `app.py` via `WEB_CONCURRENCY` check) to prevent manifest corruption.

## 10. Testing & Validation
- **E2E Tests**: `tests/test_browser_e2e.py` validates the full browser-to-result pipeline.
- **Backup Tests**: `tests/test_backup.py` ensures data integrity during export/restore.
- **Gaps**: Limited unit testing for individual tool logic; validation relies heavily on E2E tests and manual log auditing.

## 11. Known Limitations & Non-Goals
- **Concurrency**: The system uses a single-writer thread for SQLite. While this prevents locks, it creates a bottleneck for extremely high-write volumes.
- **Browser Isolation**: All tools share a single browser daemon. While `browser_lock` prevents simultaneous use, it means one tool's crash can potentially impact the daemon's stability.
- **Session ID**: Currently hardcoded to `"0"` in several API routes, limiting multi-tenant session tracking.

## 12. Change Sensitivity
- **Database Schema**: Changes to `scraped_articles` or `jobs` require migrations and may affect the `ArticleStore` reconciliation logic.
- **Manifest Format**: Modifications to `manifest.json` structure require updates to `ArticleStore` and `reconcile_delta`.
- **Worker Logic**: The polling loop in `worker.py` is tightly coupled to the `jobs` table state transitions.