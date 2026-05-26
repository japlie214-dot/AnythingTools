# AnythingTools

## 1. Project Overview
AnythingTools is a persistent, tool-augmented background service designed for high-reliability web scraping, content curation, and automated publishing to Telegram. It decouples request submission from execution to handle long-running, unstable browser tasks that would otherwise time out in a synchronous API.

### Operational Purpose
The system provides a durable execution environment for LLM-driven browser automation. It solves the problem of "fragile" web tasks by implementing state persistence, crash recovery, and Human-in-the-Loop (HITL) pausing, ensuring that progress is not lost during network failures or DOM changes.

### Explicit Non-Goals
- **Not a Chatbot**: It is an asynchronous job processor, not a real-time interactive chat interface.
- **No Frontend**: It exposes a REST API for external orchestration (e.g., by AnythingLLM).
- **No Internal LLM**: It integrates with external providers (Azure OpenAI, Chutes) via a client abstraction layer.

## 2. High-Level Architecture
The system implements a **Producer-Consumer** pattern centered around a SQLite-backed job queue.

### Major Components
- **API (`api/`)**: FastAPI interface for job enqueueing, status polling, and resumption.
- **Unified Worker Manager (`bot/engine/worker.py`)**: A singleton daemon that polls the `jobs` table and spawns isolated threads for tool execution.
- **Tool Registry (`tools/registry.py`)**: A dynamic discovery system that instantiates `BaseTool` subclasses based on their registered names.
- **Orchestrator (`bot/orchestrator_core/`)**: A middleware layer that enhances browser interaction by injecting a Semantic Object Model (SoM) into the DOM.
- **Database Layer (`database/`)**: A dual-database architecture utilizing a single-writer thread model to prevent SQLite locking contention.

### Data Flow
1. **Submission**: `POST /tools/{tool_name}` $\rightarrow$ API validates input $\rightarrow$ Job inserted into `jobs` table as `QUEUED`.
2. **Dispatch**: `UnifiedWorkerManager` polls `jobs` $\rightarrow$ Spawns execution thread $\rightarrow$ Sets status to `RUNNING`.
3. **Execution**: `Worker` $\rightarrow$ `ToolRegistry` (instantiation) $\rightarrow$ `Tool.run()` (may involve SoM injection via Orchestrator).
4. **Persistence**: Tool results are written to `jobs.result_json` and detailed progress is tracked in `job_items`.
5. **Completion**: `_do_callback_with_logging` sends the final result to the calling system via HTTP POST.

## 3. Repository Structure
- `api/`: REST endpoints. Handles job lifecycle and backup triggers.
- `bot/`:
    - `engine/`: The core polling loop (`worker.py`) and safety wrappers (`tool_runner.py`).
    - `orchestrator_core/`: Logic for SoM (Semantic Object Model) markers and browser context.
- `clients/`: LLM provider implementations (Azure, Chutes).
- `database/`: 
    - `connection.py` & `writer.py`: Implements the single-writer queue to avoid `database is locked` errors.
    - `schemas/`: SQL definitions for `jobs`, `job_items`, and `broadcast` tables.
    - `broadcast/`: Domain-specific logic for Telegram publishing and rate-limiting.
    - `backup/`: Parquet-based export/restore mechanism for data persistence.
- `deprecated/`: Historical artifacts (e.g., `bot/core/agent.py`) showing the shift from autonomous agents to programmatic tools.
- `tools/`:
    - `base.py`: Abstract base class and `ResumeReport` contracts.
    - `scraper/`: Browser-based extraction, curation, and `ResumeHandler`.
    - `publisher/`: Telegram delivery pipeline with a sliding-window rate limiter.
    - `draft_editor/`: Atomic manipulation of curated "Top 10" lists.
- `utils/`: Cross-cutting utilities (logging, artifact management, browser daemon, SoM utilities).

## 4. Core Concepts & Domain Model
### Key Abstractions
- **Job**: The primary unit of work (ULID). States: `QUEUED`, `RUNNING`, `PAUSED_FOR_HITL`, `COMPLETED`, `FAILED`, `INTERRUPTED`.
- **BaseTool**: The interface for all system capabilities.
- **SoM (Semantic Object Model)**: The process of injecting `data-ai-id` attributes into the DOM to provide the LLM with deterministic element references.
- **Resume Mechanism**: A tool-specific logic (`ResumeHandler`) that queries domain tables (e.g., `job_items`) to determine the exact point of resumption.
- **Poison Pill Protection**: A global `resume_count` in the `jobs` table that permanently fails a job if it exceeds `MAX_RESUME_ATTEMPTS`.

### Invariants
- **Single Writer**: All writes to the operational DB must pass through the `database.writer` queue.
- **Browser Lock**: Only one browser-based tool can execute at a time, enforced by `utils/browser_lock.py`.
- **Top-10 Cardinality**: The `DraftEditor` must ensure the curated list size remains exactly 10.

## 5. Detailed Behavior
### Normal Execution
1. API enqueues a job.
2. Worker picks up the job, marks it `RUNNING`, and instantiates the tool.
3. Tool executes. If browser-based, the Orchestrator injects SoM markers.
4. Tool returns a result; Worker updates the DB and triggers the HTTP callback.

### Failure Modes & Error Handling
- **Crash Recovery**: If a thread crashes, the job is marked `INTERRUPTED`. The `UnifiedWorkerManager` automatically retries these.
- **HITL Pause**: Tools can return a `PAUSED_FOR_HITL:` signal, stopping execution until a `/resume` API call is received.
- **Doom Loop Prevention**: The `/resume` endpoint increments `resume_count` and rejects the job if it exceeds the configured threshold.

## 6. Public Interfaces
### REST API
- `POST /tools/{tool_name}`: Enqueues a tool execution.
- `GET /jobs/{job_id}`: Returns status, logs, and final payload.
- `POST /jobs/{job_id}/resume`: Resumes a paused or interrupted job.
- `GET /manifest`: Returns available tools and their JSON schemas.

### Tool Registry
- `REGISTRY.create_tool_instance(name)`: Returns a tool instance.
- `REGISTRY.schema_list()`: Returns MCP-compatible tool definitions.

## 7. State, Persistence, and Data
### Storage
- **Operational DB (`sumanal.db`)**: Stores `jobs`, `job_items`, `broadcast_batches`, and `broadcast_details`.
- **Telemetry DB (`logs.db`)**: High-throughput event store.
- **Artifacts**: JSON/CSV files stored on disk, served via the API.

### Data Lifecycle
- Jobs: `QUEUED` $\rightarrow$ `RUNNING` $\rightarrow$ `COMPLETED/FAILED`.
- Broadcasts: `PENDING` $\rightarrow$ `PUBLISHING` $\rightarrow$ `COMPLETED`.

## 8. Dependencies & Integration
- **SQLite**: Primary persistence.
- **FastAPI**: API layer.
- **Azure OpenAI / Chutes**: LLM providers.
- **Botasaurus**: Browser automation.
- **PyArrow**: Used for immutable Parquet backups.

## 9. Setup, Build, and Execution
1. Install dependencies: `pip install -r requirements.txt`.
2. Configure environment variables in `config.py` (API keys, DB paths).
3. Run the application: `python app.py`.
4. The system initializes the SQLite schema on first run.

## 10. Testing & Validation
- **E2E Tests**: `tests/test_browser_e2e.py` validates the browser-tool-orchestrator loop.
- **Backup Tests**: `tests/test_backup.py` verifies Parquet export/restore integrity.
- **Gaps**: Lack of isolated unit tests for individual tools; reliance on E2E and manual validation.

## 11. Known Limitations & Non-Goals
- **SQLite Locking**: High-concurrency reads during heavy writes may still encounter timeouts despite the single-writer model.
- **Browser Stability**: Susceptible to DOM changes; partially mitigated by SoM.
- **Telegram Rate Limits**: Delivery is subject to strict pacing, managed via a sliding-window reservation system in `utils/telegram/rate_limiter.py`.

## 12. Change Sensitivity
- **Fragile Areas**: `database/writer.py` is the critical bottleneck; errors here cause total state corruption.
- **Tightly Coupled**: The `Orchestrator` depends heavily on `browser_daemon` and `som_utils.py`.
- **Extensibility**: Adding new tools is trivial via `BaseTool` and `registry.py`.