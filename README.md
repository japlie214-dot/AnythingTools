# AnythingTools — Codebase Snapshot

This README documents the repository exactly as it exists in this workspace. Every statement below is grounded in concrete, inspectable artifacts in the repository (files, functions, constants, SQL DDL). Where a claim is an inference about historical change, the claim is labeled with a confidence level and the precise code artifacts used as evidence.

This README is structured to be reconstructable: a competent engineer should be able to navigate to the referenced files, follow the code paths cited, and verify each assertion.

---

Table of contents
- Project Overview
- High-Level Architecture
- Repository Structure (top-level walkthrough)
- Core Concepts & Domain Model
- Detailed Behavior (normal execution + edge cases)
- Public Interfaces (APIs / entry points)
- State, Persistence, and Data
- Dependencies & Integration
- Setup, Build, and Execution (how to run, exact files)
- Testing & Validation
- Known Limitations & Non-Goals
- Change Sensitivity (fragile areas)
- Changes (Evolutionary Analysis, code-evidenced)

---

1. Project Overview

- What the system does (concrete):
  - This codebase implements a job orchestration and tooling runner backed by a SQLite datastore. The API accepts tool run requests and persists job metadata to the main operational SQLite file. A background manager polls the jobs table and executes tool implementations (from `tools/`) in threads. Persistent writes are serialized through a single-writer background thread; structured application logs are written to a separate logs database.
    - Evidence: API routes are implemented in [`api/routes.py`](api/routes.py:1) (see the job-creation path that INSERTs into `jobs`), the background manager is implemented in [`bot/engine/worker.py`](bot/engine/worker.py:1), the write-serialization implementation is in [`database/writer.py`](database/writer.py:1), and the separate logs writer is implemented in [`database/logs_writer.py`](database/logs_writer.py:1).

- The problem it actually solves (code-observed):
  - Provides a runtime environment to accept, persist, coordinate, and execute long-running tool jobs (scrapers, publishers, etc.) with explicit job lifecycle persisted to an on-disk database and with structured logging persisted separately.
    - Evidence: `jobs` and `job_items` DDL in [`database/schemas/jobs.py`](database/schemas/jobs.py:1), job lifecycle transitions written by [`bot/engine/worker.py`](bot/engine/worker.py:1) using `enqueue_write(...)` in [`api/routes.py`](api/routes.py:1).

- What it explicitly does NOT do (observable absences):
  - There are no container orchestration manifests, no multi-node clustering support, and no external queue system (e.g., Redis) is used — the system relies on SQLite files and in-process threads. There is no evidence of a distributed task broker.
    - Evidence: database connections use local file paths `Path("data") / "sumanal.db"` and `LOGS_DB_PATH` in [`database/connection.py`](database/connection.py:1); write queues are Python in-memory `queue.Queue` objects in [`database/writer.py`](database/writer.py:1) and [`database/logs_writer.py`](database/logs_writer.py:1).

---

2. High-Level Architecture

- Major components and the files that implement them (direct references):
  - HTTP API / router: [`api/routes.py`](api/routes.py:1) and request/response models in [`api/schemas.py`](api/schemas.py:1).
  - Tool registry and tool definitions: [`tools/registry.py`](tools/registry.py:1) and `tools/` subpackages (tool implementations live under `tools/` and are dynamically imported by the API and the worker).
  - Worker manager and job executor: [`bot/engine/worker.py`](bot/engine/worker.py:1) (class `UnifiedWorkerManager`, module-level `_manager` and `get_manager()` singleton).
  - Tool runner sandbox / safe execution: [`bot/engine/tool_runner.py`](bot/engine/tool_runner.py:1) (the worker delegates invocation to this module using `asyncio.run()` to execute tool coroutine wrappers).
  - Single-writer serializer for main DB: [`database/writer.py`](database/writer.py:1) (single thread consuming `write_queue`, implements `WriteReceipt` synchronization primitive used to wait for durability).
  - Separate logs DB writer for structured telemetry: [`database/logs_writer.py`](database/logs_writer.py:1) (bounded queue, drop counter, non-fatal overflow policy).
  - Database connection helpers and pragmas: [`database/connection.py`](database/connection.py:1) (thread-local read connections, `create_write_connection()` for writer thread, optional `sqlite_vec` extension handling).
  - Scraper pipeline: `tools/scraper/*` (notably orchestration in [`tools/scraper/task.py`](tools/scraper/task.py:1), extraction in [`tools/scraper/extraction.py`](tools/scraper/extraction.py:1), persistence in [`tools/scraper/persistence.py`](tools/scraper/persistence.py:1)).
  - Dual-stream logging API (console + DB): `utils/logger/core.py` (`SumAnalLogger.dual_log`) routes console logs to Python logging and enqueues structured logs into the logs writer queue via [`database/logs_writer.py`](database/logs_writer.py:1).

- Data flow (step-by-step, observable):
  1. External caller issues HTTP request to enqueue a tool run at [`api/routes.py`](api/routes.py:48). The route persists a new row in `jobs` using `enqueue_write("INSERT INTO jobs ...")` and returns the job id.
     - Evidence: See the INSERT call at [`api/routes.py:131`](api/routes.py:131) and the use of `enqueue_write` imported from [`database/writer.py`](database/writer.py:1).
  2. `UnifiedWorkerManager` (see [`bot/engine/worker.py`](bot/engine/worker.py:1)) polls `jobs` (SELECT from `jobs`) and, for each job to run, marks it RUNNING with `enqueue_write("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", ...)` and spawns a dedicated thread to execute that job.
     - Evidence: `UnifiedWorkerManager._run_loop` and `_run_job` call `enqueue_write(...)` at [`bot/engine/worker.py:255`](bot/engine/worker.py:255) and spawn threads with `spawn_thread_with_context(...)`.
  3. The job execution thread uses `tools/registry` to instantiate the tool, then delegates execution to `bot/engine/tool_runner.py` using `asyncio.run(run_tool_safely(...))`.
     - Evidence: lines where `res = asyncio.run(run_tool_safely(...))` in [`bot/engine/worker.py`](bot/engine/worker.py:1).
  4. The tool runs (tool implementation lives under `tools/`), which may interact with headful browser helper code (`utils/browser_daemon.py` + `tools/scraper/browser.py`) or with external LLM/embedding clients (`clients/llm/*`, `clients/snowflake_client.py`). Tool code updates intermediate state (job_items, scraped_articles) by calling `enqueue_write` or the high-level persistence helper in `tools/scraper/persistence.py` that constructs atomic transactions and returns a `WriteReceipt` to allow the caller to wait for persistence.
     - Evidence: [`tools/scraper/persistence.py`](tools/scraper/persistence.py:1) exposes `_sync_scraped_article_atomic`, which returns a receipt; caller waits for the receipt in [`tools/scraper/task.py`](tools/scraper/task.py:272).
  5. The single-writer thread (`database/writer.py`) serially performs SQL statements, commits, optionally resolves `WriteReceipt` objects so the original caller can block until on-disk commit.
     - Evidence: `WriteReceipt` dataclass is at [`database/writer.py:25`](database/writer.py:25) and writer loop resolves/rejects receipts after executing statements (`receipt.resolve()` / `receipt.reject(...)`).
  6. Structured logs are authored via `log.dual_log(...)` (from `utils/logger/core.py`); that function enqueues inserts into the logs writer queue via `logs_enqueue_write(...)` (implemented in [`database/logs_writer.py`](database/logs_writer.py:1)) so that logs are persisted independently of main DB writes.
     - Evidence: `SumAnalLogger.dual_log` calls `logs_enqueue_write` (see [`utils/logger/core.py:126`](utils/logger/core.py:126)).

- Execution model & concurrency primitives (explicit):
  - The system is primarily single-process and multi-threaded. The API uses `async` functions but delegates CPU/IO-bound tool execution into threads or `asyncio.run(...)` calls. The durable coordination primitive for persistence is a single, long-running writer thread (not a distributed broker).
    - Evidence: `write_queue = queue.Queue(maxsize=1000)` in [`database/writer.py`](database/writer.py:47); `UnifiedWorkerManager` uses `threading.Thread(...)` for the manager and per-job threads in [`bot/engine/worker.py`](bot/engine/worker.py:1).

---

3. Repository Structure (top-level walkthrough)

This section lists each top-level item in the repository and its precise role as evidenced by the code. Each entry links to a representative file.

- [`app.py`](app.py:1)
  - FastAPI application startup lifecycle is coordinated here (startup/shutdown hooks). The file references `log.dual_log(...)` at startup/shutdown points and imports the API routes. Use this as the process entry point for serving the web API.

- [`config.py`](config.py:1)
  - Centralized runtime configuration constants referenced by many modules (the codebase reads values from `config`). Check `bot/engine/worker.py` and `clients/*` for usage.

- [`requirements.txt`](requirements.txt:1)
  - Declares Python package dependencies used by the code (server, HTTP clients, DB libraries, etc.). Inspect it to reproduce the Python environment.

- [`api/`](api/)
  - [`api/routes.py`](api/routes.py:1): FastAPI router implementing endpoints: job creation (`/tools/{tool_name}`), backup admin (`/backup`), job status (`/jobs/{job_id}`), metrics (`/metrics`), diagnostics (`/diagnostics`), and resume (`/jobs/{job_id}/resume`). The file directly uses `enqueue_write(...)` for all persistent state changes.
  - [`api/schemas.py`](api/schemas.py:1): Pydantic models used as input/output shapes for the API.
  - [`api/telegram_client.py`](api/telegram_client.py:1), [`api/telegram_notifier.py`](api/telegram_notifier.py:1): Optional notifier integrations; used but not central to job lifecycle.

- [`bot/`](bot/)
  - [`bot/engine/worker.py`](bot/engine/worker.py:1): The worker manager, polling loop, job execution thread logic, callback logic. This is the core runtime orchestrator that claims jobs and runs them.
  - [`bot/engine/tool_runner.py`](bot/engine/tool_runner.py:1): Implements safe tool invocation wrappers used by the worker.
  - Other modules under `bot/` provide constants and orchestration utilities.

- [`clients/`](clients/)
  - `clients/llm/*` (e.g., [`clients/llm/providers/azure.py`](clients/llm/providers/azure.py:1)) and [`clients/snowflake_client.py`](clients/snowflake_client.py:1) provide external service integration (LLM providers and Snowflake-based embeddings).
  - Evidence of use: `tools/scraper/persistence.py` calls into embedding functions and catches `TimeoutError` for Snowflake embedding calls.

- [`database/`](database/)
  - [`database/connection.py`](database/connection.py:1): Connection factory and thread-local read connections; sets PRAGMAs (WAL, busy_timeout, synchronous) and conditionally loads `sqlite_vec` extension.
  - [`database/writer.py`](database/writer.py:1): Single-writer background thread and public enqueue functions (`enqueue_write`, `enqueue_execscript`, `enqueue_transaction`). Exposes `wait_for_writes()` for synchronous flush.
  - [`database/logs_writer.py`](database/logs_writer.py:1): Separate bounded queue for structured logs with drop-and-count policy and reconnect-on-errors.
  - [`database/schemas/`](database/schemas/): SQL DDL text for the canonical database schema used by the writer and initialization scripts — inspect these files to see table definitions (e.g., [`database/schemas/jobs.py`](database/schemas/jobs.py:1), [`database/schemas/logs.py`](database/schemas/logs.py:1)).
  - [`database/reader.py`](database/reader.py:1): Read helpers using `DatabaseManager.get_read_connection()`; interacts with the write generation counter to force refreshes.

- [`tools/`](tools/)
  - [`tools/registry.py`](tools/registry.py:1): The tool registration and discovery system; the API and worker use this registry to find tool modules and metadata (e.g., `module` path, `INPUT_MODEL`).
  - Tool implementations are under `tools/` (e.g., `tools/scraper/`, `tools/publisher/`, `tools/draft_editor/`), each with an implementation and sometimes `INPUT_MODEL` schema. For example, the scraper tool's orchestration code is in [`tools/scraper/task.py`](tools/scraper/task.py:1), and the lower-level browser helpers are in [`tools/scraper/browser.py`](tools/scraper/browser.py:1).
  - Deprecated implementations are collected under [`deprecated/`](deprecated/) (evidence of iterative evolution — see `deprecated/tools/*`).

- [`utils/`](utils/)
  - `utils/logger/` (core logger and handlers) implements the dual-stream logger that both emits console logs and enqueues structured logs for separate persistence. See [`utils/logger/core.py`](utils/logger/core.py:1) and [`utils/logger/state.py`](utils/logger/state.py:1).
  - `utils/browser_daemon.py` manages a headful browser daemon used by scrapers and includes driver lifecycle and `surgical_kill` behavior.
  - `utils/id_generator.py` provides `ULID.generate()` used as primary ID generator across jobs and logs.
  - `utils/hitl.py` contains human-in-the-loop primitives used by the scraper to pause and request decisions.

- [`tests/`](tests/)
  - `tests/test_backup.py` and `tests/test_browser_e2e.py` exist — see these for example usage patterns of the backup system and for an end-to-end browser-based test. They are evidence used to infer how certain subsystems should be exercised.

---

4. Core Concepts & Domain Model (observed)

- Jobs and Job Items (persistent domain objects):
  - `jobs` table: top-level job descriptor with `job_id`, `session_id`, `tool_name`, `args_json`, `status`, `retry_count`, `created_at`, `updated_at`, `result_json`. See DDL in [`database/schemas/jobs.py`](database/schemas/jobs.py:1).
  - `job_items` table: granular steps under a job (item metadata JSON, status, input/output data) — used by the scraper to track each article step. See `job_items` DDL in [`database/schemas/jobs.py`](database/schemas/jobs.py:18).

- Persistence primitives and invariants:
  - Single-writer invariant: all application writes to the main DB are serialized through a single dedicated thread reading from `write_queue` implemented in [`database/writer.py`](database/writer.py:1). The public API for persistence is `enqueue_write(...)`, `enqueue_execscript(...)`, and `enqueue_transaction(...)`.
    - Evidence: `write_queue: queue.Queue(maxsize=1000)` and writer loop handling of `(receipt, sql, params)` in [`database/writer.py`](database/writer.py:47).
  - Logs are **separated** from the main DB: `SumAnalLogger.dual_log` writes console logs and enqueues an insert into the logs writer (`logs_enqueue_write`) rather than using the main writer. See [`utils/logger/core.py`](utils/logger/core.py:1) and [`database/logs_writer.py`](database/logs_writer.py:1). This separation is enforced through two independent queues and two write threads.

- Strong consistency primitive exposed to callers: `WriteReceipt`
  - The `WriteReceipt` dataclass in [`database/writer.py`](database/writer.py:25) is an Event-based synchronization primitive returned by enqueue functions when `track=True`. Callers can block on `receipt.wait(timeout=...)` and check `receipt.error` to ensure write succeeded. The scraper uses this to make persistence synchronous for critical flushes (45s wait in [`tools/scraper/task.py`](tools/scraper/task.py:272)).

- WAL pragmas and WAL checkpointing
  - Main write connection sets `PRAGMA journal_mode = WAL` (see [`database/connection.py`](database/connection.py:88-107)). The writer loop triggers a `PRAGMA wal_checkpoint(TRUNCATE)` every 1200 seconds to control WAL growth (see [`database/writer.py:126-129`](database/writer.py:126)).

- Retry / repair behavior
  - The writer loop has logic to detect 'no such table' errors and attempt a best-effort table repair using `get_repair_script` from [`database/schemas/__init__.py`](database/schemas/__init__.py:1). It retries up to `MAX_REPAIR_RETRIES` (set in [`database/writer.py`](database/writer.py:56)).

- Logging contract
  - `SumAnalLogger.dual_log` enforces `payload` must be a non-empty dict (it raises `TypeError` otherwise) — see the strict contract in [`utils/logger/core.py`](utils/logger/core.py:43-56).

---

5. Detailed Behavior

Normal execution (precise, step-by-step):
1. HTTP client POSTs to `POST /tools/{tool_name}` (`api/routes.py`). Request body shape is `JobCreateRequest` in [`api/schemas.py`](api/schemas.py:7). The route validates the arguments (if the tool module exposes `INPUT_MODEL`) and then persists a row into `jobs` using `enqueue_write(...)` (see [`api/routes.py:131`](api/routes.py:131)).
2. `UnifiedWorkerManager` polls `jobs` table for jobs with status `QUEUED` or `INTERRUPTED` (see SQL selection in [`bot/engine/worker.py:214-219`](bot/engine/worker.py:214)). If found, the manager marks the job `RUNNING` (via `enqueue_write`) and spawns a job thread.
3. The job thread obtains a tool instance via `REGISTRY.create_tool_instance(tool_name)` and delegates execution to `bot/engine/tool_runner.py` via `asyncio.run(run_tool_safely(...))` (see [`bot/engine/worker.py:334-335`](bot/engine/worker.py:334)).
4. Tool logic (for example `tools/scraper/task.py`) may call into browser helpers (`tools/scraper/browser.py`, `utils/browser_daemon.py`) to navigate pages and extract content. Processing functions return results which the tool code feeds into persistence helper functions (e.g., `_sync_scraped_article_atomic` in [`tools/scraper/persistence.py`](tools/scraper/persistence.py:54)).
5. The persistence helper composes an atomic set of statements (INSERT/UPDATE of `scraped_articles`, vector insert/delete statements, and `job_items` updates) and enqueues them as a transaction via `enqueue_transaction(..., track=True)` in the writer. The worker (tool code) blocks on the returned `WriteReceipt` to ensure commit is durable before proceeding (typical wait is 45s in the scraper).
6. The single-writer thread executes the transaction, commits, and resolves the `WriteReceipt` so the caller continues.
7. Tools that need to call back to an external endpoint use the `callback` logic in the worker (`_do_callback_with_logging`) which uses `httpx` to POST to a configured URL (see [`bot/engine/worker.py:45-55`](bot/engine/worker.py:45)).

Edge cases and failure modes (observed in code):
- Write queue overflow: `enqueue_*` functions call `write_queue.put_nowait(...)` and on `queue.Full` they log a warning and, if tracking was requested, reject the corresponding `WriteReceipt` with `RuntimeError('Write queue full')` (see [`database/writer.py:298-306`](database/writer.py:298)).
- Logs queue overflow: `database/logs_writer.py` uses a bounded queue `maxsize=10000`, but instead of terminating, it increments `_logs_dropped_count` when full; it deliberately does not crash the process (drop-and-count policy) (see [`database/logs_writer.py:14, 116-121`](database/logs_writer.py:14)).
- Writer reconnection: after several consecutive errors (>= 3), the writer will attempt to re-create the write connection (see [`database/writer.py:251-262`](database/writer.py:251)). The logs writer has a similar reconnect-on-errors loop.
- No-such-table repair attempt: the writer recognizes "no such table" errors and will try to obtain a repair DDL from [`database/schemas/__init__.py`](database/schemas/__init__.py:62) and execute it. If there is no script, it gives up and rejects the receipt.

Configuration paths and toggles (observable):
- The system reads many runtime constants from [`config.py`](config.py:1). The worker’s callback behavior depends on `config.ANYTHINGLLM_BASE_URL`, `config.ANYTHINGLLM_API_KEY`, and timeouts (see `bot/engine/worker.py`).
- The optional `sqlite_vec` extension is loaded only if `sqlite_vec` imports successfully — the code sets a module-level `SQLITE_VEC_AVAILABLE` flag in [`database/connection.py`](database/connection.py:16-22) and `database/schemas/__init__.py` conditionally includes vector DDL depending on that flag.

---

6. Public Interfaces

- HTTP API endpoints (exact list and models):
  - `POST /tools/{tool_name}`: create a job; request model `JobCreateRequest` (`api/schemas.py`) and response `JobCreateResponse`.
    - Writes a DB row via `enqueue_write(...)` in [`api/routes.py:131`](api/routes.py:131).
  - `GET /jobs/{job_id}`: returns job status and job logs; response model `JobStatusResponse` (`api/schemas.py`).
  - `DELETE /jobs/{job_id}`: request job cancellation. The handler updates `jobs` row with status `CANCELLING` and enqueues a cancellation log insert into the logs DB.
  - Backup endpoints: `POST /backup/export`, `GET /backup/status`, `POST /backup/restore` implemented in [`api/routes.py`](api/routes.py:150).
  - `GET /metrics` (legacy) and `GET /diagnostics` (returns queue metrics via [`database/diagnostics.py`](database/diagnostics.py:1)).
  - `POST /jobs/{job_id}/resume`: tries to import `tools.{tool_name}.resume` and call resume handler to check resume state before re-queuing (see [`api/routes.py:361`](api/routes.py:361)).

- Tool implementation interface (observable pattern):
  - Tools are dynamically imported and are expected to register themselves in `tools/registry.py`. The API may use an `INPUT_MODEL` attribute on a tool module for Pydantic validation. The worker expects `REGISTRY.create_tool_instance(tool_name)` and that `run_tool_safely` can call into the tool instance.
    - Evidence: `module = importlib.import_module(meta.get("module"))` and `InputModel = getattr(module, "INPUT_MODEL", None)` in [`api/routes.py`](api/routes.py:72-76) and `REGISTRY.create_tool_instance(tool_name)` in [`bot/engine/worker.py`](bot/engine/worker.py:325).

- No CLI interface is evident in top-level scripts (there is no `console_scripts` entry in the repo). The primary process entrypoint for serving the API is [`app.py`](app.py:1).

---

7. State, Persistence, and Data

- Databases and file locations:
  - Operational DB file: `data/sumanal.db` (path assembled in [`database/connection.py`](database/connection.py:25)).
  - Logs DB file: `data/logs.db` (path assembled in [`database/connection.py`](database/connection.py:26)).

- Table formats and fields (select, concrete examples):
  - `jobs` table (DDL in [`database/schemas/jobs.py`](database/schemas/jobs.py:3)): `job_id TEXT PRIMARY KEY`, `session_id`, `tool_name`, `args_json`, `status`, `retry_count`, timestamps, `result_json`.
  - `job_items` table (DDL in same file): contains JSON fields `item_metadata`, `input_data`, `output_data`, `status`, `updated_at`, `job_id` foreign key.
  - `logs` table DDL is in [`database/schemas/logs.py`](database/schemas/logs.py:1) (refer to that file for exact columns — note `id` is the primary id used by logging inserts).

- Data formats: many columns store JSON strings (`args_json`, `item_metadata`, `output_data`, `payload_json` for log payloads). Code uses `json.dumps` and `json.loads` at call sites (e.g., [`api/routes.py`](api/routes.py:131), [`tools/scraper/task.py`](tools/scraper/task.py:121)).

- State lifecycle and cleanup:
  - The system uses `enqueue_write` to apply all durable changes; readers use `DatabaseManager.get_read_connection()` which refreshes per-thread read connections when the writer's generation increases. There are no automatic archive or compaction scripts present; WAL checkpointing is done opportunistically by the writer thread.
    - Evidence: `DatabaseManager.get_read_connection()` consults `get_write_generation()` (see [`database/connection.py:37-43`](database/connection.py:37)) and the writer updates `_write_generation` upon commits in [`database/writer.py`](database/writer.py:160-174).

---

8. Dependencies & Integration (observable)

- Python-level packages are declared in [`requirements.txt`](requirements.txt:1). Notable direct usage in code:
  - `fastapi` (API router usage in [`api/routes.py`](api/routes.py:1)); `pydantic` (models in `api/schemas.py`) is used for request/response validation.
  - `httpx` used for external callbacks in [`bot/engine/worker.py`](bot/engine/worker.py:121).
  - `sqlite3` is used via Python stdlib for DB access; optional `sqlite_vec` extension is loaded when available (`database/connection.py`).
  - `concurrent.futures` and `threading` are used widely for thread-based concurrency.
  - `snowflake` (client integration) is used by embedding helper code (`clients/snowflake_client.py`) and wrapped in `utils/vector_search.py`.

- Coupling points and environment assumptions:
  - The code assumes local disk access is available for `data/` (see `DB_PATH = Path("data") / "sumanal.db"` in [`database/connection.py`](database/connection.py:25)).
  - External callbacks depend on `config.ANYTHINGLLM_BASE_URL` and `config.ANYTHINGLLM_API_KEY` configuration to be present for callbacks to execute (`bot/engine/worker.py`).

---

9. Setup, Build, and Execution (exact steps to run from this repository state)

These steps are derived from the files present and conventional usage in the code; adapt to your environment as necessary:

1. Create a Python 3.10+ virtual environment and install dependencies from [`requirements.txt`](requirements.txt:1):

   - python -m venv .venv
   - .\.venv\Scripts\activate (Windows) or source .venv/bin/activate (Unix)
   - pip install -r requirements.txt

   (The exact package list is in [`requirements.txt`](requirements.txt:1).)

2. Provide runtime configuration:
   - Edit or export any required runtime values expected by `config.py` (not all are environment-driven in the code; `bot/engine/worker.py` expects `ANYTHINGLLM_BASE_URL`, `ANYTHINGLLM_API_KEY` for callbacks if used).

3. Initialize DB schema if not present:
   - The code base provides schema DDL under `database/schemas/` and a `get_init_script()` helper in [`database/schemas/__init__.py`](database/schemas/__init__.py:39) to stitch the master init script. There is startup logic in `utils/startup/database.py` that expects to run at application start.

4. Start the server API (typical):
   - `uvicorn app:app --reload` or `python app.py` depending on your deployment pattern — `app.py` contains the FastAPI app and startup hooks.

5. Enqueue a tool using the API `POST /tools/{tool_name}` with JSON body matching `api/schemas.JobCreateRequest`.

---

10. Testing & Validation (what tests exist and what they cover)

- Existing tests:
  - `tests/test_backup.py`: exercises backup/export/restore flows. See file for concrete assertions.
  - `tests/test_browser_e2e.py`: an end-to-end test that interacts with browser-oriented code paths (evidence that a headful browser path is exercised in tests).

- How to run tests: run `pytest` in the repository root (standard pattern — tests are located under `tests/`).

- Gaps visible in repository:
  - No comprehensive unit-test coverage is present for the single-writer queue and the logs writer (no mocks shown around `database/writer.py` in `tests/`).
  - Many `tools/*` implementations are not covered by unit tests in `tests/` (some e2e only). This is visible by the sparse set of test files.

---

11. Known Limitations & Non-Goals (explicit, code-evident)

- Single-process assumptions: the code relies on local SQLite files and in-process queues. It is not designed for multi-process concurrency across hosts (no distributed queue). This is enforced by the use of `sqlite3` file connections and in-memory `queue.Queue` objects. See [`database/connection.py`](database/connection.py:58) and [`database/writer.py`](database/writer.py:47).

- Logs/backpressure policy: logs queue overflow is handled by dropping entries and incrementing a counter rather than blocking or expanding capacity. That means observed logs may be lost under extreme load. See [`database/logs_writer.py`](database/logs_writer.py:14-16).

- Optional vector extension: vector table behavior depends on whether the optional `sqlite_vec` extension is loadable at runtime. If the extension is unavailable, the schema-building code in [`database/schemas/__init__.py`](database/schemas/__init__.py:46-53) falls back to a minimal table shape. This means vector performance/behavior depends on environment.

---

12. Change Sensitivity (fragile or tightly-coupled parts)

- Single-writer thread and writer queue shape: many parts of the code assume a specific task tuple shape `(receipt, sql, params)`. Changing the writer API shape would require coordinated changes in `enqueue_write`, `enqueue_transaction`, `enqueue_execscript`, and every caller (e.g., `tools/scraper/persistence.py`) and the worker. See [`database/writer.py`](database/writer.py:284-351) and consumer sites such as [`tools/scraper/persistence.py`](tools/scraper/persistence.py:54).

- Dual-logging contract: `SumAnalLogger.dual_log` mandates `payload` be a non-empty dict (it raises otherwise). Upstream code must construct payloads accordingly. Changing the signature or behavior of `dual_log` is high-impact across many modules (`utils/logger/core.py` is referenced in dozens of files).

- DB schema DDL centralization: the `database/schemas/*` set is the source of truth for init/repair scripts. Modifying the schema set without adjusting `_attempt_table_repair` and init scripts would cause runtime errors. See [`database/schemas/__init__.py`](database/schemas/__init__.py:39).