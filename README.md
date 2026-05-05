Project: AnythingTools
=====================

This repository is a self-contained tool-hosting and job-execution service that runs "tools" (pluggable worker-executable modules) and exposes them via an HTTP API. The codebase contains an HTTP server, a job queue persisted in SQLite, two background writer threads (one for application writes and a separate high-throughput writer for structured logs), a worker manager that claims and executes queued jobs, and a browser daemon used by browser-capable tools (notably the `scraper`).

This README documents the repository as it exists now (no speculation). Every referenced file is shown as an exact code reference so the reader can inspect the implementation directly.

Important top-level files you should inspect first:
- [`app.py`](app.py:1)
- [`config.py`](config.py:1)
- [`api/routes.py`](api/routes.py:1)
- [`bot/engine/worker.py`](bot/engine/worker.py:1)
- [`tools/registry.py`](tools/registry.py:1)
- [`tools/scraper/tool.py`](tools/scraper/tool.py:1)
- [`tools/scraper/task.py`](tools/scraper/task.py:1)
- [`database/writer.py`](database/writer.py:1)
- [`database/logs_writer.py`](database/logs_writer.py:1)
- [`database/connection.py`](database/connection.py:1)
- [`utils/browser_daemon.py`](utils/browser_daemon.py:1)
- [`utils/browser_lock.py`](utils/browser_lock.py:1)
- [`utils/error_export.py`](utils/error_export.py:1)
- [`tools/base.py`](tools/base.py:1)

1. Project Overview
-------------------
Concrete, operational description (evidence-based):
- The repository implements an HTTP API (FastAPI application started from [`app.py`](app.py:1) and [`api/routes.py`](api/routes.py:1)) that accepts job requests for named tools and persists those requests into an on-disk SQLite job table. See the SQL used in [`api/routes.py`](api/routes.py:1) when enqueuing jobs.
- A background writer thread serializes all database writes to the primary SQLite database (`data/sumanal.db`) using a bounded queue. The writer is implemented in [`database/writer.py`](database/writer.py:1) using a `write_queue` and the `db_writer_worker` loop.
- A separate logs subsystem writes structured log entries to a dedicated `logs.db` via a distinct, high-throughput writer implemented in [`database/logs_writer.py`](database/logs_writer.py:1) (the `logs_write_queue` and `logs_writer_worker`). The application pushes structured log entries into that queue through the dual logger in [`utils/logger/core.py`](utils/logger/core.py:1).
- A worker manager (`UnifiedWorkerManager`) polls the `jobs` table and executes tool instances in isolated threads. Execution and lifecycle transitions (QUEUED → RUNNING → COMPLETED/PENDING_CALLBACK/FAILED/INTERRUPTED/ABANDONED) are visible in [`bot/engine/worker.py`](bot/engine/worker.py:1).
- Browser-capable tools (e.g. the scraper) interact with a long-lived Chrome driver managed by a singleton manager in [`utils/browser_daemon.py`](utils/browser_daemon.py:1). Mutual exclusion across browser tasks is enforced by a synchronous `BrowserLockProxy` in [`utils/browser_lock.py`](utils/browser_lock.py:1) which wraps a `threading.Lock`.

What the system actually solves (behavioral):
- It accepts requests to run specific tool implementations (for example, the `scraper` tool), persists a durable job record, and ensures a background manager will claim and run the work. See the full enqueue flow in [`api/routes.py`](api/routes.py:1) and the worker claim/execute flow in [`bot/engine/worker.py`](bot/engine/worker.py:1).

What the system explicitly does NOT do (evidence):
- It does not perform runtime hot-reload of the tool registry. The current `ToolRegistry` uses a `_loaded` flag and `load_all(force=False)` semantics; the registry is loaded at startup via [`utils/startup/registry.py`](utils/startup/registry.py:1). See [`tools/registry.py`](tools/registry.py:1) for the `_loaded` short-circuit.
- It is not a clustered, distributed job queue. The worker manager polls a local SQLite `jobs` table and uses local thread-based execution (no distributed coordination). See [`bot/engine/worker.py`](bot/engine/worker.py:1) and [`database/connection.py`](database/connection.py:1).
- It does not persist arbitrary tool artifacts into external blob storage by default. Artifacts are written to an `artifacts/` directory via [`tools/scraper/tool.py`](tools/scraper/tool.py:1) (or the artifact manager). There are helpers to form artifact URLs, but there is no built-in S3/Cloud provider integration in the codebase (search for any cloud-specific storage code and you will find none).

2. High-Level Architecture
--------------------------
Major components and responsibilities (explicit file-based evidence):
- HTTP API server — [`app.py`](app.py:1) sets up the FastAPI app and the application lifespan. Router definitions and public endpoints are implemented in [`api/routes.py`](api/routes.py:1).
- Tool Registry — [`tools/registry.py`](tools/registry.py:1) discovers and registers concrete `BaseTool` subclasses from the local `tools/` package. The registry exposes schema lists (`schema_list`) and a manifest helper for external callers.
- Worker Manager — [`bot/engine/worker.py`](bot/engine/worker.py:1) polls the `jobs` table and spawns threads to execute tools. It manages lifecycle transitions and callback logic.
- Tool execution wrapper — [`bot/engine/tool_runner.py`](bot/engine/tool_runner.py:1) provides centralized error handling when invoking a `BaseTool` implementation.
- Tool implementations — The `tools/` package contains tool modules. The `scraper` tool ([`tools/scraper/tool.py`](tools/scraper/tool.py:1) + [`tools/scraper/task.py`](tools/scraper/task.py:1) and extraction helpers in [`tools/scraper/extraction.py`](tools/scraper/extraction.py:1)) is the most complete example: an entry-point wrapper (`ScraperTool`) and a browser-driven task implementation (`_run_botasaurus_scraper` in `task.py`).
- Database writers — Two specialized writer threads serialize writes: the application writer in [`database/writer.py`](database/writer.py:1) (for `sumanal.db`) and the logs writer in [`database/logs_writer.py`](database/logs_writer.py:1) (for `logs.db`).
- Browser daemon — [`utils/browser_daemon.py`](utils/browser_daemon.py:1) manages Chrome sessions, warmup, and surgical process termination. Consumer tools call `get_or_create_driver()` to obtain a `Driver` instance.
- Logging and log exports — The structured logger in [`utils/logger/core.py`](utils/logger/core.py:1) writes JSON payloads to the logs queue; fatal job failures are exported to text files by functions in [`utils/error_export.py`](utils/error_export.py:1).

Data flow (end-to-end, step-by-step) — follow these specific code points to trace the flow:
1. Client POST -> enqueue API: Client posts to `POST /api/tools/{tool_name}` handled by [`api/routes.py`](api/routes.py:1). The endpoint validates input (optional `INPUT_MODEL`) and writes a job row using `enqueue_write(...)` (see the SQL string in that file).
2. DB write serialization: `enqueue_write` enqueues the SQL tuple to the bounded `write_queue` used by the writer thread in [`database/writer.py`](database/writer.py:1). The writer thread executes statements sequentially and increments a generation token (see `_write_generation`). Readers use `get_write_generation()` to determine when to refresh read connections (see [`database/connection.py`](database/connection.py:1)).
3. Worker polling and claim: The `UnifiedWorkerManager` (constructed as a module-level singleton in [`bot/engine/worker.py`](bot/engine/worker.py:1)) periodically polls the `jobs` table (SQL shown in `_run_loop`) to find `QUEUED`/`INTERRUPTED`/ready `PENDING_CALLBACK` jobs and spawns threads via `spawn_thread_with_context` to execute them in `_run_job`.
4. Tool instantiation and execution: The worker uses the `REGISTRY` (`tools/registry.py`), creates a tool instance (`create_tool_instance`) and calls `run_tool_safely` which calls `tool.execute` → `BaseTool.execute` → `tool.run`. See `bot/engine/tool_runner.py` and `tools/base.py`.
5. Browser-bound tools: `ScraperTool` obtains a `Driver` from the browser daemon (`utils/browser_daemon.py`) and acquires `browser_lock` (see [`utils/browser_lock.py`](utils/browser_lock.py:1)) so only one browser task runs at a time.
6. Tool output and callback: After tool completes, the worker normalizes the result, writes `result_json` to the jobs table, and calls `_do_callback_with_logging` to post structured callbacks to the external `AnythingLLM` endpoint (`config.ANYTHINGLLM_BASE_URL`) if configured. The callback sends a markdown message and never includes raw Base64 attachments (this is enforced in `_do_callback_with_logging` in [`bot/engine/worker.py`](bot/engine/worker.py:1)).
7. Finalization and log export: For terminal failure states, the worker waits for the `logs_write_queue` to drain (up to a 60s timeout) and then calls `export_job_logs_to_file(job_id, final_status)` located in [`utils/error_export.py`](utils/error_export.py:1) to persist a job-level text log export.

Execution model & lifecycle: The system is event-driven around the `jobs` table but implemented as a local poller (threaded), not a push/subscribe distributed queue. Lifespan and graceful shutdown are orchestrated in [`app.py`](app.py:1) (see the lifespan context where startup tasks run and where the shutdown sequence stops the worker manager, signals cancellations, drains jobs (60s max), shuts down the browser daemon, then waits for the writers to finish).

3. Repository Structure (top-level walkthrough)
---------------------------------------------
Top-level layout (all paths are repository-relative):

- [`app.py`](app.py:1) — FastAPI application entrypoint and lifecycle (startup/shutdown) orchestration.
- [`config.py`](config.py:1) — Environment-backed configuration defaults. This module is imported widely; settings such as `ANYTHINGLLM_BASE_URL`, `TELEMETRY_DRY_RUN`, and `CHROME_USER_DATA_DIR` are defined here.
- [`api/`](api:1) — HTTP route definitions and API-related helpers.
  - [`api/routes.py`](api/routes.py:1) — The primary HTTP endpoints for job submission, backups, manifest, job status, and metrics.
  - [`api/schemas.py`](api/schemas.py:1) — Pydantic request/response models referenced by the routes.
- [`bot/`](bot:1) — Worker and orchestrator orchestration code.
  - [`bot/engine/worker.py`](bot/engine/worker.py:1) — Unified worker manager and execution loop.
  - [`bot/engine/tool_runner.py`](bot/engine/tool_runner.py:1) — Safe execution wrapper for tools.
  - [`bot/orchestrator_core/`](bot/orchestrator_core:1) — SoM-aware orchestrator router used by orchestrated tool execution.
- [`database/`](database:1) — DB connection and writer infrastructure and schemas.
  - [`database/connection.py`](database/connection.py:1) — Encapsulates read/write SQLite connection creation and read-connection refresh via generation tokens. Uses `sumanal.db` and `logs.db` files.
  - [`database/writer.py`](database/writer.py:1) — Main `write_queue` and writer thread (`db_writer_worker`) which executes SQL tasks deterministically.
  - [`database/logs_writer.py`](database/logs_writer.py:1) — Specialized log writer for `logs.db` with its own `logs_write_queue` and stricter overflow handling.
  - [`database/schemas/`](database/schemas:1) — SQL schema definitions and repair scripts used by the writer when `no such table` errors occur.
- [`tools/`](tools:1) — Tool plugins (each tool is expected to provide a `BaseTool` subclass and potentially a `Skill` module for metadata).
  - [`tools/registry.py`](tools/registry.py:1) — Lightweight discovery of whitelisted core tools and a manifest interface.
  - Tool modules: `tools/scraper/`, `tools/draft_editor/`, `tools/publisher/`, `tools/batch_reader/` — `scraper` is the most complete and is implemented across `task.py`, `tool.py`, `extraction.py`, `persistence.py`, and related prompt files.
- [`utils/`](utils:1) — Miscellaneous helpers: logging, browser management, text processing, artifact management, and startup hooks.
  - [`utils/browser_daemon.py`](utils/browser_daemon.py:1) — Browser session lifecycle manager.
  - [`utils/browser_lock.py`](utils/browser_lock.py:1) — Synchronous cross-thread lock for browser-bound tools.
  - [`utils/error_export.py`](utils/error_export.py:1) — Utilities to export logs to text files (job-level and error-level exports).
  - [`utils/logger/core.py`](utils/logger/core.py:1) — Structured dual logger that writes both to console and to the logs writer queue.
  - [`utils/startup/`](utils/startup:1) — Startup orchestration; `run_startup` composes steps such as DB init, migrations, tool registry load, and browser warmup.
- [`deprecated/`](deprecated:1) — Deprecated, legacy modules kept alongside the main code (evidence of prior architecture evolution). These files are not referenced by the main execution paths.
- [`tests/`](tests:1) — A small test surface. Not comprehensive; see `tests/test_backup.py` and `tests/test_browser_e2e.py`.

Why certain directories exist (observed patterns):
- `deprecated/` contains many older tool implementations and indicates in-place refactors rather than repository deletion.
- `tools/` groups concrete tool implementations; `tools/registry.py` is intentionally conservative and whitelists a small set of core tools.

4. Core Concepts & Domain Model
------------------------------
Key runtime artifacts and their canonical forms (based on SQL and code usage):

- Job record (in `jobs` table): columns used in queries and inserts include (see SQL in [`api/routes.py`](api/routes.py:1) and [`database/schemas/jobs.py`](database/schemas/jobs.py:1)) — `job_id`, `session_id`, `tool_name`, `args_json`, `status`, `result_json`, `retry_count`, `created_at`, `updated_at`. The code assumes `args_json` and `result_json` are JSON-serializable strings.

- Job item record (for fine-grained step tracking): used by scraper partial-resume logic (see queries in [`tools/scraper/task.py`](tools/scraper/task.py:1)). The scraping pipeline writes `job_items` rows using `add_job_item(...)` and updates them with `update_item_status(...)`.

- Logs records (in `logs` table): the logger enqueues tuples with fields: `id`, `job_id`, `tag`, `level`, `status_state`, `message`, `payload_json`, `event_id`, `error_json`, `timestamp`. The log-export utilities query these fields directly — see [`utils/logger/core.py`](utils/logger/core.py:1) and [`utils/error_export.py`](utils/error_export.py:1).

- ToolResult / BaseTool contract: [`tools/base.py`](tools/base.py:1) defines `ToolResult` (a dataclass with `output: str`, `success: bool`, `attachment_paths: list[str] | None`, `event_id`) and `BaseTool.execute`/`run` semantics. The system expects `execute()` to return a `ToolResult` and `run()` to produce a string suitable for callback. `ToolResult.output` is often JSON-parseable but not required to be.

Invariants enforced by the code (observable):
- Only one browser-capable tool may run at a time — enforced by `browser_lock` in [`utils/browser_lock.py`](utils/browser_lock.py:1) and used by tools like [`tools/scraper/tool.py`](tools/scraper/tool.py:1).
- Database reader connections are long-lived per-thread and are refreshed when the writer increments a generation counter. Reader refresh logic is in [`database/connection.py`](database/connection.py:1).
- Logs are written to a separate database with pragmas tuned for high-throughput writes (`PRAGMA synchronous = OFF`) – see [`database/connection.py`](database/connection.py:1) for the logs manager configuration.

5. Detailed Behavior and Failure Modes
-------------------------------------
Normal run (end-to-end, mapped to files):
1. POST /api/tools/{tool_name} -> [`api/routes.py`](api/routes.py:1) validates input (optionally using a tool-supplied `INPUT_MODEL`) and persists a `jobs` row via `enqueue_write()`.
2. `database/writer.py`'s writer thread dequeues writes and safely executes SQL on `sumanal.db`. If a `no such table` error occurs, the writer attempts a repair script via `database/schemas` (see `_attempt_table_repair`).
3. `UnifiedWorkerManager` periodically polls (`bot/engine/worker.py`), claims jobs, moves the job to `RUNNING` (via `enqueue_write`), and spawns a thread to run the tool.
4. Tools are executed under `run_tool_safely` (`bot/engine/tool_runner.py`), which catches unhandled exceptions and writes a `ToolRunner:Error` log to `logs.db`.
5. If the tool is browser-bound, it acquires the `browser_lock` for the entire `run()` invocation; scrapers use the `botasaurus` driver implementation via [`utils/browser_daemon.py`](utils/browser_daemon.py:1) and page/som extraction helpers in [`tools/scraper/extraction.py`](tools/scraper/extraction.py:1).
6. Tool results are normalized and either: (a) posted back to an external AnythingLLM callback endpoint, or (b) written to the job `result_json` and job `status` updated. The callback code is in `_do_callback_with_logging` inside [`bot/engine/worker.py`](bot/engine/worker.py:1).

Failure modes & error handling (explicitly evidenced):
- Logs writer overflow is treated as fatal. In [`database/logs_writer.py`](database/logs_writer.py:1), if the logs queue is full and cannot be enqueued after a 5s block, the process issues a SIGTERM to itself. This is explicit and indicates the system favors fail-fast over silent loss of structured logs.
- The DB writer (`database/writer.py`) uses a bounded queue and will attempt to restart the writer thread if missing; in `enqueue_write`, if the queue is full it currently logs a warning and drops the write (see `enqueue_write`), which means non-critical writes can be lost under pressure. That behavior is explicit in the code.
- Worker crashes increment a per-job system error counter and may mark the job `INTERRUPTED` or `ABANDONED` after multiple attempts (see error handling in `_run_job` in [`bot/engine/worker.py`](bot/engine/worker.py:1)).
- For terminal job failures (`FAILED`, `ABANDONED`, `PARTIAL`) the worker attempts to wait for the `logs_write_queue` to drain (up to 60s) before generating a job-level text export (`utils/error_export.py`), to avoid missing final fatal log entries.
- Browser warmup failures set a `CRITICAL_FAILURE` status on the daemon and are treated as fatal in startup; see [`utils/browser_daemon.py`](utils/browser_daemon.py:1) and [`utils/startup/browser.py`](utils/startup/browser.py:1).

6. Public Interfaces
--------------------
HTTP endpoints (exposed under `/api` in [`app.py`](app.py:1) via `api/routes.py`):
- POST /api/tools/{tool_name} — enqueue a job. Input shape is `JobCreateRequest` in [`api/schemas.py`](api/schemas.py:1). The handler validates input (using a tool's optional `INPUT_MODEL`) and enqueues the job.
- POST /api/backup/export — starts a background export via `database.backup.runner.BackupRunner.run`. See [`api/routes.py`](api/routes.py:1).
- POST /api/backup/restore — schedules a restore and enforces `browser_lock` in the handler.
- GET /api/manifest — returns the tool manifest produced by [`tools/registry.py`](tools/registry.py:1).
- GET /api/jobs/{job_id} — returns job status including recent logs from `logs.db` (see SQL in [`api/routes.py`](api/routes.py:1)).
- DELETE /api/jobs/{job_id} — request cancellation: sets job status = `CANCELLING` and writes a cancellation log to `logs.db`.

Worker-facing APIs (internal):
- `REGISTRY.create_tool_instance(name)` — instantiate a tool class (factory in [`tools/registry.py`](tools/registry.py:1)).
- `enqueue_write(sql, params)` and `enqueue_transaction(...)` — schedule DB writes to the single-writer thread (`database/writer.py`).
- `logs_enqueue_write(sql, params)` — schedule log writes to `logs.db` (`database/logs_writer.py`).

Tool contract (developer-facing):
- `BaseTool` (`tools/base.py`) requires a `run()` coroutine and `execute()` wrapper returns a `ToolResult` dataclass. Tools should use `self.status()` for standardized telemetry messages and rely on `BaseTool.execute` to flush the in-memory tool log buffer to `logs.db` via `flush_tool_buffer_to_job_logs` in [`utils/logger/core.py`](utils/logger/core.py:1).

7. State, Persistence, and Data
--------------------------------
Databases and files (explicit):
- Primary application DB: `data/sumanal.db` (path defined in [`database/connection.py`](database/connection.py:1)). Used for persistent state: `jobs`, `scraped_articles`, `job_items`, etc.
- Logs DB: `data/logs.db` — an independent SQLite instance tuned for high-throughput writes. The schema is consulted by the logger and by `utils/error_export.py` when assembling text exports.
- Artifacts directory: `artifacts/` subdirectories per tool (written via `write_artifact` in [`tools/scraper/tool.py`](tools/scraper/tool.py:1)). Current implementation performs atomic writes using a temporary file and `os.replace`.

Data lifecycles (evidence):
- Writes are serialized by background writer threads. The code calls `await wait_for_writes()` and `shutdown_writer()` in the application shutdown sequence to wait for outstanding writes (see [`app.py`](app.py:1)).
- Logs buffer flush: `BaseTool.execute` uses a per-job in-memory `_tool_log_buffer` and flushes it to the logs writer queue at the end of execute (see [`tools/base.py`](tools/base.py:1) referencing [`utils/logger/core.py`](utils/logger/core.py:1)).
- Cleanup tasks: startup cleanup removes `*.tmp.parquet` files and kills zombie Chrome processes conservatively (see [`utils/startup/cleanup.py`](utils/startup/cleanup.py:1)).

8. Dependencies & Integration
-----------------------------
Explicitly referenced libraries (observed via import statements):
- `fastapi` and `uvicorn` (server and routing) — imports used in [`app.py`](app.py:1) and [`api/routes.py`](api/routes.py:1).
- `httpx` — used by `_do_callback_with_logging` for HTTP callbacks (`bot/engine/worker.py`).
- `bs4` (BeautifulSoup) — used by scraper extraction (`tools/scraper/extraction.py`).
- `botasaurus` — the code imports `botasaurus.browser.Driver` in [`tools/scraper/task.py`](tools/scraper/task.py:1) indicating a heavy dependency on that external driver abstraction.
- `psutil` — optionally used by browser process inspectors (`utils/browser_daemon.py`) and startup cleanup (`utils/startup/cleanup.py`). The code guards for `ImportError` and proceeds if `psutil` is not present in some places.
- `dotenv` — `config.py` calls `load_dotenv()`.
- `openai` — used by [`tools/scraper/extraction.py`](tools/scraper/extraction.py:1) for error detection (e.g., `BadRequestError`) when interacting with an LLM provider.
- `sqlite_vec` — optional extension detection in [`database/connection.py`](database/connection.py:1) to provide vector search features if available.

Coupling points & environment assumptions (evidence):
- The system assumes a POSIX/Windows filesystem for `data/` and `artifacts/` directories (created via `Path(...).mkdir(parents=True, exist_ok=True)`).
- `CHROME_USER_DATA_DIR` environment variable provides a Chrome profile path consumed by the browser daemon (`config.py` and [`utils/browser_daemon.py`](utils/browser_daemon.py:1)).
- External callback integration: `ANYTHINGLLM_BASE_URL`, `ANYTHINGLLM_API_KEY` are read from `config.py` and used to post structured callback messages in [`bot/engine/worker.py`](bot/engine/worker.py:1).

9. Setup, Build, and Execution (as-is)
--------------------------------------
Minimum steps (derived from the repository files):
1. Create a Python environment with the packages required by imports above (e.g., `fastapi`, `uvicorn`, `httpx`, `bs4`, `python-dotenv`, and any LLM provider client used). The repository includes `requirements.txt` but the project relies on the imports shown in code; inspect [`requirements.txt`](requirements.txt:1) for an authoritative list.
2. Edit environment variables as needed (see [`config.py`](config.py:1) for names and defaults).
3. Initialize local data directories (the code will create `data/` and `artifacts/` automatically on first run).
4. Run migrations / DB initialization. The startup sequence in [`utils/startup/core.py`](utils/startup/core.py:1) and [`utils/startup/database.py`](utils/startup/database.py:1) performs initialization when `app` is started. The recommended way is: run the FastAPI app via `uvicorn app:app --reload --port 8000` as the code comments indicate in [`app.py`](app.py:1).

Platform constraints and explicit timeouts:
- The browser warmup step has a 60-second timeout (see [`utils/startup/browser.py`](utils/startup/browser.py:1)).
- The worker drain on shutdown waits up to 60 seconds for active jobs to finish (see [`app.py`](app.py:1)).

10. Testing & Validation
------------------------
Tests present in the repo (evidence):
- [`tests/test_backup.py`](tests/test_backup.py:1)
- [`tests/test_browser_e2e.py`](tests/test_browser_e2e.py:1)

Coverage notes (observable):
- Tests are sparse and focused on a few integration behaviors such as backup and a browser E2E path. There is no comprehensive unit-test suite that isolates all critical modules (e.g., registry, worker manager, DB writer) visible in the `tests/` directory. This is an explicit repository observation.

How to run tests: use the typical `pytest` invocation in the project root. See `tests/` for the exact files and what they assert.

11. Known Limitations & Non-Goals (direct evidence)
---------------------------------------------------
- Logs writer overflow is fatal (see `database/logs_writer.py`, where queue overflow triggers SIGTERM). This is an explicit design choice in the code.
- The main DB writer may drop writes when under pressure — `enqueue_write` logs and drops writes when `write_queue.put_nowait` would raise `queue.Full`. This presents a known, observable trade-off (reduced latency vs guaranteed persistence) in the current implementation.
- Tool registry is intentionally conservative and not dynamic at runtime — `tools/registry.py` implements whitelisting and has a `_loaded` gate that prevents repeated discovery unless `force=True`.
- The scraper implements an in-module checkpoint/resume façade, but the current tool wrapper retains a no-op shim (`_check_step` and `_get_step_output`) meaning resumability is visually present in the codebase but functionally disabled. The shim is evidence that resumability was considered but the active code path forces full execution.

12. Change Sensitivity (fragile areas)
--------------------------------------
Areas where small changes are likely to cause broad effects (observed code coupling):
- The single-writer DB pattern in [`database/writer.py`](database/writer.py:1) couples write ordering and reader visibility to an in-memory generation token. Changing writer concurrency or swapping to a multi-writer design requires rethinking reader refresh logic across `DatabaseManager` and `LogsDatabaseManager`.
- The dual-log path: console-then-logs writer design in [`utils/logger/core.py`](utils/logger/core.py:1) assumes the `logs_write_queue` and `logs_writer_worker` exist and are tuned for high throughput. Changing logging semantics will require touching many call sites.
- Browser lifecycle: `utils/browser_daemon.py`](utils/browser_daemon.py:1) centralizes driver creation and surgical process kills. Changes there affect all browser-capable tools (notably `scraper`). The `browser_lock` concept spans multiple modules; removing it would require careful coordination.