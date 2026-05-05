# AnythingTools — Codebase Reference (Exact, Current State)

This README describes the repository as it exists right now. Every statement below is grounded on files present under the repository root and in-code comments; references to source files are provided for verification. Where the code is ambiguous or shows multiple possible interpretations, that ambiguity is explicitly stated.

- Quick pointer to verify the application entrypoint: [`app.py`](app.py:1)
- Configuration and environment-driven flags: [`config.py`](config.py:1)

---

1. Project Overview

- What the system does (concrete, observable behavior):
  - Hosts an HTTP API implemented with FastAPI: see [`app.py`](app.py:1). The application defines a lifespan (startup/shutdown) hook that runs an asynchronous startup routine and performs an orderly shutdown sequence that attempts to stop a worker manager, cancel worker flags, drain active jobs, and shut down database writer threads (see the lifespan implementation at [`app.py`](app.py:43)).
  - Contains a modular set of "tools" (see the `tools/` package) that implement domain logic. The shipped tool with the largest surface area is the `scraper` tool, implemented under [`tools/scraper/`](tools/scraper/tool.py:1), which implements an end-to-end scraping pipeline: extraction, curation, artifact writing, and backup coordination.
  - Provides background persistence and single-writer semantics for database writes via a background writer thread implemented in [`database/writer.py`](database/writer.py:1). There is an additional specialized writer for logs in [`database/logs_writer.py`](database/logs_writer.py:1).
  - Exposes Telegram integration code under [`utils/telegram/`](utils/telegram/telegram_client.py:1) that uses `python-telegram-bot` exceptions (e.g., `RetryAfter`) and a rate limiter in [`utils/telegram/rate_limiter.py`](utils/telegram/rate_limiter.py:1).

- What problem it actually solves (observed):
  - The code orchestrates automation jobs (scraping/publishing) started via HTTP calls (API routes are located under `api/`), executes them asynchronously via a worker manager, persists results to local SQLite-backed tables, and provides artifact files under the `artifacts/` directory when produced by tools like the `scraper` (artifact writing helper in [`tools/scraper/tool.py`](tools/scraper/tool.py:311)).

- What it explicitly does NOT do (observed omissions / absence):
  - There is no frontend/UI code in the repository (no `frontend/` dir). The public surface is an HTTP API as in [`app.py`](app.py:100).
  - There is no evidence of a distributed queueing system (no Celery, RabbitMQ client code). Background work is implemented with in-process threads and async tasks using the worker manager and the single-writer queue in [`database/writer.py`](database/writer.py:1).

---

2. High-Level Architecture

- Major components and responsibilities (file references):
  - HTTP API and application lifecycle: [`app.py`](app.py:1) and routes under `api/` (e.g., [`api/routes.py`](api/routes.py:1)).
  - Startup orchestration and process-level initialization: [`utils/startup/`](utils/startup/__init__.py:1) and notably [`utils/startup/database.py`](utils/startup/database.py:1) (database writer startup, logs schema initialization, etc.).
  - Logging subsystem (console + structured persistence): [`utils/logger/`](utils/logger/__init__.py:1) with core semantics in [`utils/logger/core.py`](utils/logger/core.py:1) and formatting/handlers in the same package.
  - Persistence and single-writer queue: [`database/writer.py`](database/writer.py:1) (main writer), [`database/logs_writer.py`](database/logs_writer.py:1) (logs persistence), and schema definitions under [`database/schemas/`](database/schemas/__init__.py:1).
  - Tools/Plugin registry: [`tools/registry.py`](tools/registry.py:1) and per-tool modules (example: [`tools/scraper/`](tools/scraper/tool.py:1)).
  - Worker orchestration (in-process): [`bot/engine/worker.py`](bot/engine/worker.py:1) and supporting orchestrator code under `bot/orchestrator_core/`.
  - External clients and integrations: `clients/` for LLM providers and Snowflake (e.g., [`clients/llm/providers/azure.py`](clients/llm/providers/azure.py:1), [`clients/snowflake_client.py`](clients/snowflake_client.py:1)).

- Data flow (step-by-step, concrete):
  1. An API request hits the FastAPI server (`app.py`), and route handlers (under `api/`) may create or enqueue a job. The app enforces an API key dependency (`app.py` lines 23–36 referencing [`config.py`](config.py:1)).
  2. Jobs are recorded in SQLite-backed persistence (schemas in [`database/schemas/`](database/schemas/__init__.py:1)). Job items and statuses are managed via the job queue helpers under [`database/job_queue.py`](database/job_queue.py:1).
  3. A worker manager (see [`bot/engine/worker.py`](bot/engine/worker.py:1)) polls or consumes the job queue, instantiates a tool via the `tools/` registry (`tools/registry.py`), and invokes the assigned tool's execution method (example: `ScraperTool._run_internal` in [`tools/scraper/tool.py`](tools/scraper/tool.py:33)).
  4. Tools perform I/O (browsers, LLM calls); when writes to durable stores are needed, they call helper APIs that enqueue writes to the single-writer queue implemented in [`database/writer.py`](database/writer.py:1). Logs are dispatched through the structured logger (see [`utils/logger/core.py`](utils/logger/core.py:1)).
  5. Long-running or error states are persisted in the job/job_items tables and (for backups) written to Parquet using backup helpers under `database/backup/` (see [`database/backup/runner.py`](database/backup/runner.py:1)).

- Control flow / runtime model:
  - The system is a hybrid: an HTTP server (async via FastAPI) that spawns/uses synchronous background threads (writer thread) and asynchronous worker tasks/threads for job execution — see `asynccontextmanager` usage in [`app.py`](app.py:43) and the background writer thread in [`database/writer.py`](database/writer.py:66).
  - The logger implements a dual-stream model: console/master file vs. logs DB; structured entries are enqueued to a dedicated logs-writer queue (`utils/logger/core.py` and `database/logs_writer.py`).

---

3. Repository Structure (top-level walkthrough)

All top-level folders and critical files (present now):

- `app.py` — FastAPI app entrypoint and lifespan hooks. See [`app.py`](app.py:1).
- `config.py` — Environment-driven configuration and feature toggles (API key, telemetry flags, external service keys). See [`config.py`](config.py:1).
- `api/` — HTTP route implementations and API wiring. Notably [`api/routes.py`](api/routes.py:1) mounts the main router used by the application.
- `bot/` — Worker and orchestrator layer (manager, worker thread logic) used at runtime by the app. Example: [`bot/engine/worker.py`](bot/engine/worker.py:1).
- `clients/` — External service clients (LLM, Snowflake, etc.). Example providers in [`clients/llm/providers/`](clients/llm/providers/azure.py:1) and a Snowflake client in [`clients/snowflake_client.py`](clients/snowflake_client.py:1).
- `database/` — Persistence layer, single-writer queue, read/write helpers, backup/restore. Key files: [`database/writer.py`](database/writer.py:1), [`database/logs_writer.py`](database/logs_writer.py:1), schema scripts in `database/schemas/` (see [`database/schemas/logs.py`](database/schemas/logs.py:1)).
- `tools/` — A registry and collection of tool modules; tools are functionally *plugins* that implement domain tasks. The `scraper` tool is at [`tools/scraper/tool.py`](tools/scraper/tool.py:1) with browser helpers at [`tools/scraper/browser.py`](tools/scraper/browser.py:1) and persistence internals at [`tools/scraper/persistence.py`](tools/scraper/persistence.py:1).
- `utils/` — Reusable utilities (logging, browser helpers, HITL helpers, startup orchestration). Examples: [`utils/logger/`](utils/logger/__init__.py:1), [`utils/startup/database.py`](utils/startup/database.py:1), [`utils/hitl.py`](utils/hitl.py:1), and browser helpers under [`utils/browser_daemon.py`](utils/browser_daemon.py:1).
- `tests/` — Automated tests present (at least `tests/test_backup.py` and `tests/test_browser_e2e.py`). These indicate unit/integration test artifacts exist but do not guarantee complete coverage.
- `database/backup/` — Parquet export/import code and runner utilities (`database/backup/exporter.py` and `database/backup/runner.py`).
- `deprecated/` — Legacy code and vestigial modules retained for historical or referential reasons (multiple submodules present). The folder contains multiple older tools and shows the codebase carried forward historical implementations.

Why these directories exist (concrete reasons inferred from file contents):
- `utils/logger/` contains a complete structured logging subsystem that writes both to console handlers and to a dedicated logs db; the code includes a programmer-facing "contract" requiring a non-empty structured payload (see [`utils/logger/__init__.py`](utils/logger/__init__.py:1) and [`utils/logger/core.py`](utils/logger/core.py:43)).
- `database/` separates the main application schema (`database/writer.py`) from logs persistence (`database/logs_writer.py`) indicating deliberate separation of concerns in persistence for audit/logging.

---

4. Core Concepts & Domain Model

- Jobs & job_items: the code uses a job-based unit of work model stored in persistent tables. Evidence: presence of `database/job_queue.py` and the worker code in `bot/engine/worker.py`.

- Tools / registry: `tools/` modules implement units of executable work; `tools/registry.py` exposes a registry used by the HTTP manifest endpoint (see [`app.py`](app.py:128) which calls `REGISTRY.load_all()` before returning the manifest).

- Logs: There are two log destinations: console/master file (via Python logging) and structured logs persisted to `logs` table in a logs DB handled by [`database/logs_writer.py`](database/logs_writer.py:1). The logging system enforces that log entries must include a non-empty `payload` dictionary — see the hard check in [`utils/logger/core.py`](utils/logger/core.py:43–56).

- Artifacts: Tools may write artifact files to an `artifacts/` directory using helpers like `tools/scraper/tool.py`'s `write_artifact` (see [`tools/scraper/tool.py`](tools/scraper/tool.py:311)). Artifacts are recorded into an in-memory list and incorporated into final job payloads.

- Concurrency invariants:
  - Single writer: All durable writes are serialized through `enqueue_write` and the background writer thread in [`database/writer.py`](database/writer.py:66).
  - Browser operations are guarded by a `browser_lock` (`utils/browser_lock.py`) to avoid concurrent driver access.
  - Logger readiness gating: writes to logs DB are avoided until `verify_logs_readiness()` succeeds (see [`utils/startup/database.py`](utils/startup/database.py:75)).

---

5. Detailed Behavior (Normal execution and failure modes)

- Normal startup sequence (as implemented now):
  1. The FastAPI app's lifespan hook calls [`utils/startup.run_startup()`] (see [`app.py`](app.py:47)). The startup package wires multiple phases.
  2. The database layer startup routine attempts a "Fresh Start" wipes of `logs.db` (see [`utils/startup/database.py`](utils/startup/database.py:16)). If the file unlink succeeds, the system proceeds. If unlink fails, the startup routine falls back to synchronously dropping and recreating the `logs` table using `LogsDatabaseManager.create_write_connection()` and executing the logs init script (see [`utils/startup/database.py`](utils/startup/database.py:33–41)). This fallback is a deliberate, synchronous remediation for file unlink failures.
  3. The database writer thread(s) are started (`database/writer.start_writer()`; logs writer started via `database/logs_writer.start_logs_writer()`). The startup verifies logs readiness (`verify_logs_readiness()`) before proceeding (see [`utils/startup/database.py`](utils/startup/database.py:73–81)).
  4. The worker manager (if any) starts and the system becomes able to process queued jobs.

- What happens when a tool runs (scraper example):
  - The `ScraperTool.run` method delegates to `_run_internal` which orchestrates extraction, curation, artifact writing, backup and finalization (see [`tools/scraper/tool.py`](tools/scraper/tool.py:33–279)).
  - Extraction results are stored as artifacts via `write_artifact` (see [`tools/scraper/tool.py`](tools/scraper/tool.py:311)). Persisted artifacts are appended to a `artifacts_written` list and included in final job payloads.
  - Writes to persistent storage are performed via `enqueue_write` (see [`database/writer.py`](database/writer.py:141)), ensuring a single-writer serialized queue.

- Error modes and logging behavior:
  - The `dual_log` API enforces that `payload` is a non-empty dict; failing to pass a valid payload causes a `TypeError` (see [`utils/logger/core.py`](utils/logger/core.py:43–56)). Many modules guard against this by constructing informative `payload` dicts for every `dual_log` invocation.
  - Startup includes a robust fallback to re-create the `logs` table synchronously if unlinking `logs.db` fails (see [`utils/startup/database.py`](utils/startup/database.py:33–41)). This reduces a class of filesystem permission errors at startup.
  - Write failures in the writer thread attempt repair for missing tables via `_attempt_table_repair` (see [`database/writer.py`](database/writer.py:42–55)). Transaction failures and foreign-key constraint failures are logged with explanatory `payload` entries.

- Configuration toggles affecting behavior (concrete):
  - `SUMANAL_ALLOW_SCHEMA_RESET` environment variable is consulted in [`database/management/lifecycle.py`](database/management/lifecycle.py:22) to decide whether a destructive reset is allowed (this is controlled through `os.getenv("SUMANAL_ALLOW_SCHEMA_RESET", "0") == "1"`).
  - `TELEMETRY_DRY_RUN` in [`config.py`](config.py:1) can cause the `ScraperTool` to short-circuit to a dry-run failure payload (see [`tools/scraper/tool.py`](tools/scraper/tool.py:83–86)).

---

6. Public Interfaces (entry points, APIs, CLI)

- HTTP API: The app exposes routes under `/api` via the router in [`api/routes.py`](api/routes.py:1). The app implements API key verification with a header `X-API-Key` validated against [`config.py`](config.py:9) (see [`app.py`](app.py:23–36)).
- Manifest endpoint: A public manifest is available at the public router `/api/manifest` which calls into the tools registry to enumerate available tools (see [`app.py`](app.py:128–132)).
- CLI / run command: The top-level comment in [`app.py`](app.py:1) documents starting the HTTP server with `python -m uvicorn app:app --reload --port 8000` — this is the supported local development invocation.

---

7. State, Persistence, and Data

- Where state is stored: local SQLite files (main DB and a separate logs DB). Database path constants are defined in `database/connection.py` (see [`database/connection.py`](database/connection.py:1)).
- Schemas: Table DDL and initialization scripts live under [`database/schemas/`](database/schemas/__init__.py:1) (for example logs schema at [`database/schemas/logs.py`](database/schemas/logs.py:1)).
- Backups & exports: Parquet backup/export code is under [`database/backup/`](database/backup/exporter.py:1), and the backup runner is at [`database/backup/runner.py`](database/backup/runner.py:1).
- Artifact format: Artifacts are produced as files by tools (example `write_artifact` returns a `Path` and writes JSON in [`tools/scraper/tool.py`](tools/scraper/tool.py:311)).
- Migration/cleanup: There is lifecycle/reconciler code for schema validation and automatic repairs in [`database/management/lifecycle.py`](database/management/lifecycle.py:1) and [`database/management/reconciler.py`](database/management/reconciler.py:1).

---

8. Dependencies & Integration (actually relied-upon libraries)

Observably imported libraries in the codebase include (examples, based on in-file imports):
- `fastapi` and `uvicorn` — used by the HTTP entrypoint (`app.py` imports `FastAPI` and documents `uvicorn`) → required for running the API.
- `python-telegram-bot` — code imports `telegram.Bot`, `telegram.error.RetryAfter` in [`utils/telegram/telegram_client.py`](utils/telegram/telegram_client.py:4–6).
- `bs4` (BeautifulSoup) — used for HTML extraction in [`tools/scraper/browser.py`](tools/scraper/browser.py:10).
- `httpx` — imported by [`tools/scraper/tool.py`](tools/scraper/tool.py:8), used for HTTP requests.
- `python-dotenv` — loaded in [`config.py`](config.py:4) via `load_dotenv()`.
- The code also relies on numpy/struct-style binary packing for embeddings in [`tools/scraper/persistence.py`](tools/scraper/persistence.py:100) and Snowflake client integration (`clients/snowflake_client.py`).

Coupling and assumptions:
- The logging system assumes availability of a writable logs DB and a running logs writer (see [`utils/logger/core.py`](utils/logger/core.py:65–69)). Startup includes code to ensure the logs writer is running (see [`utils/startup/database.py`](utils/startup/database.py:73–81)).
- The system expects local filesystem write permissions for `artifacts/` (artifact writing helper in [`tools/scraper/tool.py`](tools/scraper/tool.py:311)).

---

9. Setup, Build, and Execution (exact steps observed from code)

Minimal steps to run from a clean clone (based strictly on repository evidence):
  1. Create a Python virtualenv and install packages from `requirements.txt` (the repository contains a `requirements.txt`).
  2. Set a working API key via the environment variable `API_KEY` (the code uses `config.py` to read `API_KEY`).
  3. Start the app in development: `python -m uvicorn app:app --reload --port 8000` (documented in the top-of-file comment in [`app.py`](app.py:1)).

Notes and platform constraints:
- The startup code includes platform-aware remediation for failing to unlink `logs.db` (see [`utils/startup/database.py`](utils/startup/database.py:24–42)) — this is necessary on Windows where files can be locked.
- The code performs file I/O (artifact writing) and uses native SQLite; no containers or cloud orchestration is enforced by code artifacts.

---

10. Testing & Validation

- Test artifacts: `tests/test_backup.py` and `tests/test_browser_e2e.py` exist under `tests/`. Running `pytest` at the repository root is the conventional invocation; these test files indicate there is at least coverage for backup and a browser end-to-end flow.
- Gaps visible from the repository: there are many modules (logging, tools, database lifecycle) with complex logic but only two test files visible in `tests/` — this suggests incomplete coverage for many critical paths (e.g., `utils/logger`, `database/writer`, `tools/*` usage). This is an explicit observation, not a judgment.

---

11. Known Limitations & Non-Goals (directly evidenced)

- Logging contract: `dual_log` enforces `payload` to be a non-empty dict and will raise a `TypeError` if violated — which forces all call sites to supply structured payloads and means ad-hoc logging (no payload) is not supported by the code as it stands (`utils/logger/core.py`: check lines 43–56).
- The presence of `deprecated/` demonstrates legacy code retained for reference rather than active use; modules in that directory are not visible to runtime wiring by default and should be considered vestigial.
- Single-machine operation: the repository uses in-process worker manager and a single-writer SQLite queue; there is no evidence of clustering, distributed locks, or external queueing systems. This constrains scalability to the process/machine boundary.

---

12. Change Sensitivity (fragile, tightly-coupled parts)

- `utils/logger/core.py` and the `dual_log` contract are high-impact: many modules call `dual_log` and rely on the logs writer. Changing the signature of `dual_log` or the logs writer semantics will require wide changes across the codebase (`utils/logger/core.py` + every file that calls `dual_log`). Evidence: `dual_log` is invoked across many modules (search results show many call sites).
- `database/writer.py` and the single-writer queue are central. Any change to write semantics or to the writer thread state machine requires coordinated changes across modules that call `enqueue_write` — see [`database/writer.py`](database/writer.py:141) and call sites throughout the code.
- Browser concurrency: `browser_lock` and the `browser_daemon` code coordinate a single set of driver resources; adding parallel browser sessions would likely be invasive.