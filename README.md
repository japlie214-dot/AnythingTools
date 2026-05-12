# AnythingTools: Technical Specification & Architecture

## 1. Project Overview
AnythingTools is a high-reliability, tool-augmented intelligence system designed to execute complex, multi-step web scraping, data curation, and intelligence delivery pipelines. It operates as a backend service that integrates with LLM orchestrators (e.g., AnythingLLM) via a job-based asynchronous API.

### Operational Purpose
The system solves the problem of unreliable, long-running web automation tasks by implementing a "Job-Worker" architecture with strong persistence, crash recovery, and a single-writer database pattern to ensure data integrity in a highly concurrent environment.

### Explicit Non-Goals
- **Real-time Interactive Chat:** The system does not manage chat sessions; it executes tools and reports results back to a caller.
- **Direct LLM Orchestration:** It does not decide *which* tool to run; it provides the execution environment and the tools themselves.
- **Distributed Scaling:** The current architecture is designed for a single-node deployment with a local SQLite database.

---

## 2. High-Level Architecture

### Major Components
- **API Layer (`api/`):** A FastAPI wrapper that enqueues jobs and provides status/metrics endpoints.
- **Unified Worker Manager (`bot/engine/worker.py`):** A singleton polling loop that monitors the `jobs` table and spawns execution threads for `QUEUED` or `INTERRUPTED` tasks.
- **Tool Registry (`tools/registry.py`):** A dynamic discovery system that loads tool classes from the `tools/` directory and instantiates them on demand.
- **Single-Writer DB Layer (`database/writer.py`):** A serialized background thread that handles all writes to the primary SQLite DB to prevent `SQLITE_BUSY` locks.
- **SoM (Semantic Object Model) Orchestrator (`bot/orchestrator_core/`):** A specialized execution wrapper for browser tools that injects semantic markers into the DOM to improve LLM targeting precision.
- **Hydration System (`utils/startup/hydration.py`):** A startup process that restores database state from Parquet backups using PyArrow.

### Data Flow
1. **Request:** API receives a POST to `/tools/{tool_name}` $\rightarrow$ Job is inserted into `jobs` table with status `QUEUED`.
2. **Polling:** `UnifiedWorkerManager` detects the job $\rightarrow$ Marks status as `RUNNING` $\rightarrow$ Spawns a thread.
3. **Execution:** `tool_runner.py` invokes the tool. If it's a browser tool, the `OrchestratorRouter` injects SoM markers into the browser.
4. **Persistence:** Tool results and telemetry are sent to the `write_queue` $\rightarrow$ `db_writer_worker` persists them to SQLite.
5. **Callback:** Upon completion, `_do_callback_with_logging` sends a structured markdown result to the AnythingLLM API.
6. **Logging:** All events are routed through `utils/logger/core.py` to both a standard log and a separate `logs.db`.

---

## 3. Repository Structure

- `app.py`: Entry point. Manages the FastAPI lifespan and the sequential startup pipeline.
- `api/`: 
    - `routes.py`: API endpoints for job management, backups, and diagnostics.
    - `schemas.py`: Pydantic models for request/response validation.
- `bot/`:
    - `engine/`: Core execution logic. `worker.py` is the heartbeat of the system.
    - `orchestrator_core/`: Logic for SoM (Semantic Object Model) injection and context building.
- `clients/`: External service integrations.
    - `snowflake_client.py`: Handles embeddings via Snowflake Cortex AI.
    - `llm/`: Provider-agnostic LLM client factory (Azure, Chutes).
- `database/`:
    - `connection.py`: Manages thread-local read connections and `sqlite-vec` extension loading.
    - `writer.py`: The single-writer thread implementing `WriteReceipt` for read-after-write consistency.
    - `articles/`: Logic for managing scraped content and Parquet streaming.
    - `backup/`: Export/Restore logic for Parquet-based state backups.
    - `schemas/`: SQL definitions for jobs, logs, and vector tables.
- `tools/`:
    - `base.py`: `BaseTool` abstract class.
    - `registry.py`: Dynamic tool discovery and instantiation.
    - `scraper/`: Complex pipeline for web extraction, curation, and validation.
    - `publisher/`: Intelligence delivery pipeline to Telegram.
- `utils/`:
    - `logger/`: Custom "Rule of Three" logging system (`Category:SubCategory:Action`).
    - `startup/`: Orchestrated startup sequence (Cleanup $\rightarrow$ Migration $\rightarrow$ Hydration $\rightarrow$ Recovery).
    - `vector_search.py`: Semantic retrieval using `sqlite-vec`.
    - `text_processing.py`: HTML cleaning and Telegram message splitting.
- `deprecated/`: Archive of legacy patterns (e.g., `Skill.py` descriptors).

---

## 4. Core Concepts & Domain Model

### The "Rule of Three" Logging
Logging is strictly categorized as `Category:SubCategory:Action` (e.g., `Worker:Job:Recovery`). This allows for precise filtering and automated observability.

### Single-Writer Pattern
To overcome SQLite's concurrency limitations, the system uses a dedicated writer thread. Callers use `enqueue_write()`. If a caller needs to ensure a write is committed before reading, it uses a `WriteReceipt` to block until the writer thread resolves the event.

### SoM (Semantic Object Model)
For browser automation, the system doesn't just pass raw HTML. It injects `data-ai-id` attributes into the DOM, creating a map of the page that the LLM can reference using precise integer IDs, significantly reducing "hallucinated" selectors.

### Job Lifecycle
`PENDING` $\rightarrow$ `QUEUED` $\rightarrow$ `RUNNING` $\rightarrow$ (`COMPLETED` | `FAILED` | `PARTIAL` | `INTERRUPTED`).
- `INTERRUPTED`: A job that crashed or was killed. The `UnifiedWorkerManager` prioritizes these for recovery.
- `PARTIAL`: A job that completed its primary task but failed its callback to the orchestrator.

---

## 5. Detailed Behavior

### Startup Sequence
The system follows a strict tiered startup in `utils/startup/__init__.py`:
1. **Concurrent Tier:** Mounts artifacts, cleans zombie Chrome processes, and initializes the DB layer.
2. **Sequential Tier:** Runs migrations $\rightarrow$ Hydrates state from Parquet backups $\rightarrow$ Validates vector tables $\rightarrow$ Recovers interrupted jobs.
3. **Application Tier:** Loads the tool registry and warms up the browser daemon.

### Error Handling & Recovery
- **Transient Errors:** The worker implements exponential backoff for HTTP callbacks.
- **Poisoned Connections:** If the DB writer encounters 3 consecutive errors, it closes the connection and reconnects.
- **Fatal Vector Errors:** If `sqlite-vec` encounters a fatal blob length error, the transaction is rejected to prevent DB corruption.
- **HITL (Human-In-The-Loop):** Tools can raise a `PAUSED_FOR_HITL` exception, which moves the job to a paused state and blocks the thread until an operator resolves the challenge.

---

## 6. Public Interfaces

### REST API
- `POST /tools/{tool_name}`: Enqueues a tool execution. Returns `job_id`.
- `GET /jobs/{job_id}`: Returns current status and associated logs.
- `POST /backup/export`: Triggers a Parquet export of the current state.
- `POST /backup/restore`: Triggers a restoration from backup.
- `GET /metrics`: Returns internal system health and queue depths.

### Tool Interface
All tools must inherit from `BaseTool` and implement `async def execute(args, telemetry, **kwargs)`. They must return a `ToolResult` object.

---

## 7. State, Persistence, and Data

### Databases
- **Primary DB (`main.db`):** Stores jobs, articles, and long-term memories. Uses WAL mode.
- **Logs DB (`logs.db`):** A high-throughput database dedicated to telemetry.

### Vector Search
Implemented via the `sqlite-vec` extension. Embeddings (1024-dim) are stored as blobs. Semantic search is performed using cosine similarity within SQLite.

### Backup & Hydration
State is persisted as Parquet files. During startup, `hydrate_from_backup` streams these files into SQLite using PyArrow to avoid OOM errors and minimize downtime.

---

## 8. Dependencies & Integration

- **FastAPI/Uvicorn:** Web interface.
- **Botasaurus:** Browser automation and driver management.
- **PyArrow/Pandas:** High-performance data streaming for Parquet backups.
- **Snowflake Cortex:** Vector embedding generation.
- **sqlite-vec:** Vector similarity search within SQLite.
- **python-telegram-bot:** Intelligence delivery.

---

## 9. Setup, Build, and Execution

1. **Environment:** Install Python 3.10+.
2. **Dependencies:** `pip install -r requirements.txt`.
3. **Binary Assets:** Install the `sqlite-vec` extension binary in the library path.
4. **Configuration:** Create a `.env` file with `API_KEY`, `AZURE_OPENAI_KEY`, and `SNOWFLAKE_ACCOUNT`.
5. **Execution:** `python app.py`.

---

## 10. Testing & Validation
- **E2E Browser Tests:** `tests/test_browser_e2e.py` validates the browser daemon and SoM injection.
- **Backup Tests:** `tests/test_backup.py` validates Parquet export/import integrity.
- **Gaps:** No unit tests for individual tools; testing is primarily integration-based.

---

## 11. Known Limitations & Non-Goals
- **SQLite Lock Contention:** While the single-writer pattern mitigates this, extremely high write volumes may still saturate the `write_queue`.
- **Memory Spikes:** Large Parquet restorations are throttled, but very large datasets may still cause memory pressure during the `Symmetric` phase of hydration.
- **Single-Node:** No support for distributed workers or shared database clusters.

---

## 12. Change Sensitivity
- **Database Schemas:** High sensitivity. Changes to `database/schemas/` require coordinated migration scripts.
- **Tool Registry:** Low sensitivity. New tools can be added by creating a new directory in `tools/` without modifying core logic.
- **Writer Logic:** Critical sensitivity. Any change to `database/writer.py` can introduce deadlocks or data loss.