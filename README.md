# AnythingTools

## 1. Project Overview
AnythingTools is a persistent, tool-augmented agentic system designed for high-reliability web scraping, content curation, and automated publishing to Telegram. It operates as a background worker service that executes a registry of specialized tools, managing long-running jobs with state persistence and crash recovery.

### Operational Purpose
The system solves the problem of unstable, long-running LLM-driven browser tasks by decoupling the request (API) from the execution (Worker). It provides a durable execution environment where jobs can be interrupted, paused for human-in-the-loop (HITL) intervention, or resumed from a known state.

### Explicit Non-Goals
- It is not a real-time interactive chatbot; it is an asynchronous job processor.
- It does not provide a built-in frontend; it exposes a REST API for external orchestration.
- It does not implement its own LLM; it integrates with Azure OpenAI and other providers via a client abstraction.

## 2. High-Level Architecture
The system follows a **Producer-Consumer** pattern with a shared database for state and a dedicated background thread for writes.

### Major Components
- **API (`api/`)**: A FastAPI-based interface that validates requests and enqueues jobs into the database.
- **Unified Worker Manager (`bot/engine/worker.py`)**: A singleton daemon that polls the `jobs` table, spawns execution threads, and manages job lifecycles.
- **Tool Registry (`tools/registry.py`)**: A dynamic loader that discovers `BaseTool` subclasses and manages their schemas.
- **Orchestrator (`bot/orchestrator_core/`)**: A middleware layer that injects Semantic Object Model (SoM) context into browser-based tools to improve element targeting.
- **Database Layer (`database/`)**: A dual-database setup (`sumanal.db` for operational state, `logs.db` for high-throughput telemetry) using a single-writer thread model to avoid SQLite locking contention.

### Data Flow
1. **Request**: `POST /tools/{tool_name}` $\rightarrow$ API validates args $\rightarrow$ Inserts job into `jobs` table (status: `QUEUED`).
2. **Polling**: `UnifiedWorkerManager` detects `QUEUED` or `INTERRUPTED` job $\rightarrow$ Spawns thread.
3. **Execution**: `Worker` $\rightarrow$ `ToolRegistry` (instantiate tool) $\rightarrow$ `Orchestrator` (inject SoM markers) $\rightarrow$ `Tool.execute()`.
4. **Persistence**: Tool results are written to `jobs.result_json` via `database.writer`.
5. **Callback**: `_do_callback_with_logging` sends the final result back to the calling system (e.g., AnythingLLM) via HTTP POST.

## 3. Repository Structure
- `api/`: FastAPI routes and schemas. Handles job submission, status polling, and backup triggers.
- `bot/`:
    - `engine/`: The core execution loop (`worker.py`) and safe wrapper (`tool_runner.py`).
    - `orchestrator_core/`: SoM (Semantic Object Model) logic for browser interaction.
- `clients/`: LLM provider abstractions (Azure, Chutes).
- `database/`: 
    - `connection.py`: Manages read-only and write connections.
    - `writer.py`: The single-threaded write queue implementation.
    - `schemas/`: SQL definitions for jobs, logs, and articles.
    - `broadcast/`: Specialized logic for Telegram batch publishing, including state-seeded progress tracking and rate-limited delivery.
- `deprecated/`: Contains legacy versions of the agent, tools, and modes (e.g., `bot/core/agent.py`), serving as evidence of architectural evolution.
- `tools/`:
    - `base.py`: Abstract base class for all tools.
    - `scraper/`: Complex browser-based extraction and curation logic.
    - `publisher/`: Telegram publishing pipeline.
    - `draft_editor/`: Programmatic Top-10 list manipulation.
- `utils/`: Cross-cutting concerns (logging, artifact management, text processing, browser daemon).

## 4. Core Concepts & Domain Model
### Key Abstractions
- **Job**: The unit of work. Tracked by a ULID. States include `QUEUED`, `RUNNING`, `PAUSED_FOR_HITL`, `COMPLETED`, `FAILED`, and `INTERRUPTED`.
- **BaseTool**: All tools must inherit from this, implementing `execute()`.
- **SoM (Semantic Object Model)**: A technique of injecting `data-ai-id` attributes into the DOM to allow the LLM to reference specific elements deterministically.
- **WriteReceipt**: A synchronization primitive allowing asynchronous writes to be awaited.
- **Sliding Window Rate Limiter**: A proactive traffic control mechanism that reserves future timestamps to ensure strict pacing of API requests and prevent 429 flood limits.

### Invariants
- **Single Writer**: Only one thread may write to the primary database to prevent `database is locked` errors.
- **Surgical Edits**: The `DraftEditor` strictly maintains the cardinality of the "Top 10" list.

## 5. Detailed Behavior
### Normal Execution
1. API enqueues a job.
2. Worker picks up the job and marks it `RUNNING`.
3. Tool executes; if it's a browser tool, the Orchestrator injects SoM markers.
4. Tool returns a `ToolResult`.
5. Worker updates `jobs.result_json` and triggers an HTTP callback.

### Failure Modes & Error Handling
- **Crash Recovery**: If a worker thread crashes, the job is marked `INTERRUPTED`. The manager will automatically retry these jobs.
- **HITL Pause**: If a tool returns a string containing `PAUSED_FOR_HITL:`, the worker stops execution and marks the job as such, awaiting an API `resume` call.
- **Context Blowout**: The `Top10Curator` implements a "last-error-only" retry mechanism to prevent the prompt from growing indefinitely during failures.

## 6. Public Interfaces
### REST API
- `POST /tools/{tool_name}`: Enqueues a tool execution.
- `GET /jobs/{job_id}`: Returns status, logs, and final payload.
- `POST /jobs/{job_id}/resume`: Resumes a paused/interrupted job.
- `GET /manifest`: Returns the list of available tools and their JSON schemas.

### Tool Registry
- `REGISTRY.create_tool_instance(name)`: Returns a fresh instance of a registered tool.
- `REGISTRY.schema_list()`: Returns MCP-compatible tool definitions.

## 7. State, Persistence, and Data
### Storage
- **Operational DB (`sumanal.db`)**: Stores `jobs`, `job_items`, `broadcast_batches`, and `broadcast_details`.
- **Telemetry DB (`logs.db`)**: High-throughput store for all system events.
- **Artifacts**: Files (JSON, CSV) are stored on disk and served via the API.

### Data Lifecycle
- Jobs move from `QUEUED` $\rightarrow$ `RUNNING` $\rightarrow$ `COMPLETED/FAILED`.
- Broadcast batches move from `PENDING` $\rightarrow$ `PUBLISHING` $\rightarrow$ `COMPLETED`.

## 8. Dependencies & Integration
- **SQLite**: Primary persistence. Uses `sqlite-vec` for vector search (optional).
- **FastAPI**: API layer.
- **Azure OpenAI**: Primary LLM provider.
- **Botasaurus**: Browser automation.
- **PyArrow**: Used for database backups and schema enforcement.

## 9. Setup, Build, and Execution
1. Install dependencies: `pip install -r requirements.txt`.
2. Configure environment variables in `config.py` (API keys, DB paths).
3. Run the application: `python app.py`.
4. The system automatically initializes the SQLite schema on first run.

## 10. Testing & Validation
- **E2E Tests**: `tests/test_browser_e2e.py` validates the browser-tool-orchestrator loop.
- **Backup Tests**: `tests/test_backup.py` verifies data integrity during export/restore.
- **Gaps**: No comprehensive unit tests for individual tools; validation relies on E2E and manual testing.

## 11. Known Limitations & Non-Goals
- **SQLite Locking**: Despite the single-writer thread, high-concurrency reads during heavy writes can still encounter timeouts.
- **Browser Stability**: Browser-based tools are susceptible to DOM changes, mitigated partially by SoM.
- **Memory**: The `UnifiedWorkerManager` keeps active job threads in memory; extremely large batches may increase memory pressure.
- **Telegram API Constraints**: Delivery is subject to strict rate limits (overall and per-group), managed via a sliding window reservation system.

## 12. Change Sensitivity
- **Fragile Areas**: `database/writer.py` is the most critical point; any change to the queue logic can corrupt the entire state.
- **Tightly Coupled**: The `Orchestrator` is tightly coupled to the `browser_daemon` and `utils/som_utils.py`.
- **Easy Extension**: Adding new tools is easy—simply create a `BaseTool` subclass in `tools/` and add it to the whitelist in `registry.py`.