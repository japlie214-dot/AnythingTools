# AnythingTools — Precise Codebase Snapshot

This README documents the repository exactly as it exists in this workspace. Every claim below is grounded in inspectable artifacts in the repository (files, functions, constants, SQL DDL). Where an assertion is an inference about historical change it is labeled with a confidence level and the precise code artifacts used as evidence. Follow the clickable references to verify each statement.

Table of contents
- 1. Project Overview
- 2. High-Level Architecture
- 3. Repository Structure (top-level walkthrough)
- 4. Core Concepts & Domain Model
- 5. Detailed Behavior (normal execution + edge cases)
- 6. Public Interfaces
- 7. State, Persistence, and Data
- 8. Dependencies & Integration
- 9. Setup, Build, and Execution
- 10. Testing & Validation
- 11. Known Limitations & Non-Goals
- 12. Change Sensitivity
- 13. Changes (Evolutionary Analysis from Current Code)

---

1. Project Overview

- What the system does (concrete, code-observed):
  - Accepts tool-run requests via an HTTP API, stores persistent job and item state in a local SQLite database, and executes registered tool implementations in worker threads. Evidence: API enqueue + DB persist in [`api/routes.py:131`](api/routes.py:131); worker polling + execution in [`bot/engine/worker.py:210`](bot/engine/worker.py:210).
  - Serializes all writes to the main application DB through a single dedicated writer thread that consumes a bounded queue and exposes an optional synchronization primitive (WriteReceipt). Evidence: writer loop, queue, and WriteReceipt in [`database/writer.py:47`](database/writer.py:47) and [`database/writer.py:25`](database/writer.py:25).
  - Persists structured application logs to a separate logs DB using a separate bounded writer to avoid coupling logs to the main write path. Evidence: logs writer queue and write thread in [`database/logs_writer.py:14`](database/logs_writer.py:14) and logger enqueuing in [`utils/logger/core.py:126`](utils/logger/core.py:126).

- What problem this codebase actually solves (code-observed):
  - Provides an on-disk, single-process job orchestration runtime with durability guarantees for write operations (via WriteReceipt) and a decoupled logs persistence path. Evidence: job data model DDL in [`database/schemas/jobs.py:3`](database/schemas/jobs.py:3), enqueue_write usage in [`api/routes.py:131`](api/routes.py:131), writer synchronization in [`database/writer.py:30`](database/writer.py:30).

- What it explicitly does NOT do (observable absences):
  - No distributed task broker or multi-node orchestration. The code uses local SQLite files and in-process threading rather than external queues (e.g., Redis). Evidence: DB paths in [`database/connection.py:66`](database/connection.py:66) and use of `queue.Queue` in [`database/writer.py:47`](database/writer.py:47).

---

2. High-Level Architecture

- Major components (file-level mapping):
  - HTTP API / router: [`api/routes.py:1`](api/routes.py:1) (FastAPI `APIRouter` handlers, input validation, and job enqueue logic).
  - Registry & tool discovery: [`tools/registry.py:1`](tools/registry.py:1) (tool metadata and create/inspect helpers used by API and worker).
  - Worker manager and job execution: [`bot/engine/worker.py:177`](bot/engine/worker.py:177) (`UnifiedWorkerManager`) that polls jobs and spawns per-job threads.
  - Tool runner sandbox: [`bot/engine/tool_runner.py:1`](bot/engine/tool_runner.py:1) (wrapping tool calls and capturing results).
  - Single-writer DB serializer: [`database/writer.py:108`](database/writer.py:108) (writer loop and public enqueue helpers).
  - Separate logs writer: [`database/logs_writer.py:38`](database/logs_writer.py:38) (bounded queue, writer thread, and dropped-log policy).
  - DB connection management and optional vector extension loading: [`database/connection.py:75`](database/connection.py:75).
  - Scraper tool (example tool implementation family): [`tools/scraper/task.py:24`](tools/scraper/task.py:24), [`tools/scraper/extraction.py:1`](tools/scraper/extraction.py:1), [`tools/scraper/persistence.py:1`](tools/scraper/persistence.py:1).
  - Dual-stream logging API (console + DB): [`utils/logger/core.py:27`](utils/logger/core.py:27) (`SumAnalLogger.dual_log`).

- Data flow (concrete step-by-step, with file references):
 1. Client POSTs a job to `POST /tools/{tool_name}`. Handler validates input (optionally using a tool module's `INPUT_MODEL`) and writes a `jobs` row via `enqueue_write(...)`. Evidence: [`api/routes.py:48`](api/routes.py:48), validation branch at [`api/routes.py:71`](api/routes.py:71), INSERT at [`api/routes.py:131`](api/routes.py:131).
 2. `UnifiedWorkerManager` polls `jobs` for `QUEUED`/`INTERRUPTED` items and marks them `RUNNING` via `enqueue_write(...)`, then spawns a job thread. Evidence: poll SQL in [`bot/engine/worker.py:214`](bot/engine/worker.py:214) and `enqueue_write` call at [`bot/engine/worker.py:256`](bot/engine/worker.py:256).
 3. The job thread uses the registry to instantiate the tool and invokes it through `run_tool_safely` (which uses `asyncio.run`). Evidence: registry usage at [`bot/engine/worker.py:325`](bot/engine/worker.py:325) and `asyncio.run(run_tool_safely(...))` at [`bot/engine/worker.py:334`](bot/engine/worker.py:334).
 4. Tool code may call helpers (browser automation, scraping pipelines, LLM/embedding clients) and persist results using persistence helpers that construct an atomic transaction and return a `WriteReceipt` so the tool can wait for durability. Evidence: `_sync_scraped_article_atomic(...)` returns a receipt in [`tools/scraper/persistence.py:54`](tools/scraper/persistence.py:54) and the caller waits on the receipt in [`tools/scraper/task.py:290`](tools/scraper/task.py:290).
 5. The single-writer thread dequeues tasks and executes them serially, committing and updating a write-generation counter used by read connections to refresh stale readers. Evidence: writer loop increments `_write_generation` in [`database/writer.py:155`](database/writer.py:155) and read connection refresh logic in [`database/connection.py:87`](database/connection.py:87).
 6. Logs are emitted via `log.dual_log(...)` which enqueues inserts into `logs_writer` rather than the main writer. Evidence: logger enqueues a logs write at [`utils/logger/core.py:128`](utils/logger/core.py:128) and the logs writer consumes `logs_write_queue` in [`database/logs_writer.py:56`](database/logs_writer.py:56).

- Execution model & concurrency primitives:
  - Single process, multi-threaded. Writer has a dedicated (single) non-daemon thread by default (`start_writer()` spawns `sqlite-writer` thread at [`database/writer.py:355`](database/writer.py:355)). Worker manager spawns threads per job (see [`bot/engine/worker.py:193`](bot/engine/worker.py:193)).
  - Durable synchronization for specific writes is implemented through `WriteReceipt` (see [`database/writer.py:25`](database/writer.py:25)).

---

3. Repository Structure (top-level walkthrough)

Below is every top-level directory/file that materially participates in runtime behavior, with its exact role and a primary code pointer (click to open line).

- [`app.py:1`](app.py:1)
  - FastAPI application module used as a primary process entrypoint. It wires startup/shutdown lifecycle hooks and includes the API router.

- [`config.py:1`](config.py:1)
  - Central configuration constants referenced throughout (worker timeouts, external URLs, directory paths). Search for usages in [`bot/engine/worker.py:213`](bot/engine/worker.py:213) and client modules.

- [`requirements.txt:1`](requirements.txt:1)
  - Exact dependencies present in the environment; note `openai>=1.47.0` (structured output fallbacks are present) and `sqlite-vec>=0.1.0` (optional extension). Evidence: [`requirements.txt:48`](requirements.txt:48) and [`requirements.txt:39`](requirements.txt:39).

- [`api/`]
  - [`api/routes.py:1`](api/routes.py:1): All HTTP endpoints (enqueue tools, job status, backup management, metrics, diagnostics, resume). The handlers are explicit about persistence via `enqueue_write`.
  - [`api/schemas.py:1`](api/schemas.py:1): Pydantic models used for request and response validation (referenced at validation points in [`api/routes.py:71`](api/routes.py:71)).

- [`bot/`]
  - [`bot/engine/worker.py:177`](bot/engine/worker.py:177): `UnifiedWorkerManager`, job execution lifecycle, resumption and callback retry logic.
  - [`bot/engine/tool_runner.py:1`](bot/engine/tool_runner.py:1): Runner that isolates tool execution and returns a normalized result object consumed by the worker.

- [`database/`]
  - [`database/connection.py:75`](database/connection.py:75): Thread-local read connections, `create_write_connection`, optional `sqlite_vec` loading and the module-level `_VEC_PERMANENTLY_FAILED` flag to avoid repeated load attempts.
  - [`database/writer.py:108`](database/writer.py:108): Single-writer background loop, `enqueue_write`, `enqueue_transaction`, `WriteReceipt`, WAL checkpointing, and repair attempts for missing tables.
  - [`database/logs_writer.py:14`](database/logs_writer.py:14): Separate logs DB writer with bounded queue and drop counter.
  - [`database/schemas/`](database/schemas/): Canonical DDL definitions for the main DB; `jobs` and `job_items` DDL live in [`database/schemas/jobs.py:3`](database/schemas/jobs.py:3).
  - [`database/management/reconciler.py:81`](database/management/reconciler.py:81): Schema reconciler used by startup/repair paths; contains logic to preserve FTS/vec shadow tables (including `_vector_chunks*`).

- [`tools/`]
  - [`tools/registry.py:1`](tools/registry.py:1): Tool registration/discovery used by API and worker to instantiate tools.
  - Example concrete tool family: `tools/scraper/` implementing scraping orchestration and persistence (`tools/scraper/task.py:24`, `tools/scraper/extraction.py:1`, `tools/scraper/persistence.py:1`).
  - Deprecated/legacy implementations aggregated under [`deprecated/`](deprecated/) (evidence of earlier designs and iterative refactors).

- [`utils/`]
  - `utils/logger/*`: Dual-stream logger implementation and utilities. See [`utils/logger/core.py:27`](utils/logger/core.py:27) and `flush`/buffer helpers.
  - `utils/browser_daemon.py`: headful browser daemon lifecycle and surgical kill behavior used by scraper tools (referenced by scraper code).
  - `utils/hitl.py` and `tools/scraper/extraction.py:42`](tools/scraper/extraction.py:42): Human-in-the-loop primitives (blocking stdin) used by the scraper.

- [`tests/`]
  - `tests/test_backup.py:1`](tests/test_backup.py:1) is a concrete set of unit tests that validate backup DDL and embedding byte-length invariants.

---

4. Core Concepts & Domain Model (observed)

- Jobs & job_items (canonical persisted objects):
  - `jobs` DDL: `job_id TEXT PRIMARY KEY`, `session_id`, `tool_name`, `args_json`, `status` with explicit `CHECK(...)` constraint including `'SKIPPED'`  see [`database/schemas/jobs.py:3`](database/schemas/jobs.py:3) and the `job_items` DDL at [`database/schemas/jobs.py:18`](database/schemas/jobs.py:18).
  - `job_items` records per-step metadata (JSON in `item_metadata`) and a `status` column constrained by the schema. The scraper uses `job_items` to track per-article progress; this is visible in [`tools/scraper/task.py:116`](tools/scraper/task.py:116) where items are pre-seeded and updated.

- Persistence primitives and invariants:
  - Single-writer invariant: only the writer thread executes SQL commits for the main DB; callers use `enqueue_*` helpers. Evidence: `enqueue_transaction(...)` and writer loop flow in [`database/writer.py:329`](database/writer.py:329).
  - WriteReceipt contract: callers can request tracking (blocking on `receipt.wait(timeout)`) to get durable confirmation of a write. Evidence: `WriteReceipt.wait()` and usage in `_sync_scraped_article_atomic` (`tools/scraper/persistence.py:149`](tools/scraper/persistence.py:149) and usage of wait in [`tools/scraper/task.py:300`](tools/scraper/task.py:300).

- Logging contract & separation:
  - `SumAnalLogger.dual_log` requires `payload` to be a non-empty dict or raises `TypeError`. Evidence: contract check at [`utils/logger/core.py:55`](utils/logger/core.py:55).
  - Logs persisted to `logs.db` via `logs_enqueue_write(...)` to avoid interfering with main DB writes. Evidence: enqueue call in [`utils/logger/core.py:128`](utils/logger/core.py:128) and logs writer implementation in [`database/logs_writer.py:14`](database/logs_writer.py:14).

- Vector / embedding handling:
  - Embeddings are stored in vector tables and are optional depending on `sqlite_vec` availability. The code validates embedding byte lengths and uses `utils.vector_search` helpers; embedding insertion into `scraped_articles_vec` is performed as part of atomic persistence in [`tools/scraper/persistence.py:119`](tools/scraper/persistence.py:119).
  - The writer detects vec-specific errors and logs them distinctly via `_is_vec0_error(...)` and the `DB:Writer:VecError` tag. Evidence: vector error detection in [`database/writer.py:72`](database/writer.py:72) and handling at [`database/writer.py:208`](database/writer.py:208).

---

5. Detailed Behavior

Normal execution (precise step-by-step):
1. Client POSTs to `POST /tools/{tool_name}` (`api/routes.py:48`](api/routes.py:48)). The route optionally validates `req.args` with a tool-provided `INPUT_MODEL` (if present) and persists a `jobs` row via `enqueue_write(...)` at [`api/routes.py:131`](api/routes.py:131).
2. The background manager (`UnifiedWorkerManager`) polls `jobs` with status in `('QUEUED','INTERRUPTED')` (query at [`bot/engine/worker.py:214`](bot/engine/worker.py:214)) and marks selected jobs as `RUNNING` (e.g., [`bot/engine/worker.py:256`](bot/engine/worker.py:256)).
3. The manager spawns a per-job thread and executes tool code via `run_tool_safely(...)` (`bot/engine/worker.py:334`](bot/engine/worker.py:334)).
4. Tool code uses persistence helpers (example: `_sync_scraped_article_atomic()` in [`tools/scraper/persistence.py:54`](tools/scraper/persistence.py:54)) which build a `TRANSACTION_MARKER` statements list and call `enqueue_transaction(..., track=True)`; the returned `WriteReceipt` is waited on by the caller to ensure commit durability (see waiter in [`tools/scraper/task.py:300`](tools/scraper/task.py:300)).
5. Writer thread executes transactions and increments a generation counter used by read connections to refresh thread-local connections (`database/writer.py:163`](database/writer.py:163) and [`database/connection.py:87`](database/connection.py:87)).

Edge cases and failure modes (observed):
- Write queue overflow: `enqueue_*` functions attempt `put_nowait(...)` and reject `WriteReceipt` if queue is full. Evidence: overflow handling at [`database/writer.py:293`](database/writer.py:293).
- No-such-table repair attempt: the writer matches "no such table" errors and attempts a repair script via `get_repair_script(...)`. Evidence: `_is_no_such_table_error` and `_attempt_table_repair` at [`database/writer.py:64`](database/writer.py:64) and [`database/writer.py:77`](database/writer.py:77).
- vec0/sqlite-vec problems: the writer has explicit handling for vec-related insert errors and logs detailed payload previews (`database/writer.py:208`](database/writer.py:208)).
- Human-in-the-loop behavior: the scraper's HITL implementation performs a synchronous blocking `input()` call and updates job status to `PAUSED_FOR_HITL` before waiting. This is an explicit design choice documented in [`tools/scraper/extraction.py:42`](tools/scraper/extraction.py:42) and implemented at [`tools/scraper/extraction.py:72`](tools/scraper/extraction.py:72).

Configuration paths and how they alter behavior:
- `config.py` contains toggles and external URLs used by callback and worker logic (used by [`bot/engine/worker.py:42`](bot/engine/worker.py:42)).
- `sqlite_vec` behavior is conditional on the import succeeding; `database/connection.py` sets `SQLITE_VEC_AVAILABLE` and will try to load the native extension (`database/connection.py:18`](database/connection.py:18) and `_attempt_vec_load` function at [`database/connection.py:27`](database/connection.py:27)).

---

6. Public Interfaces

- HTTP API endpoints (direct, exact references):
  - `POST /tools/{tool_name}` — enqueue job: code at [`api/routes.py:48`](api/routes.py:48) and persistent write at [`api/routes.py:131`](api/routes.py:131). Request/response models in [`api/schemas.py:7`](api/schemas.py:7).
  - `GET /jobs/{job_id}` — fetch job status and job logs via `LogsDatabaseManager.get_read_connection()` at [`api/routes.py:213`](api/routes.py:213).
  - `DELETE /jobs/{job_id}` — request cancellation (writes `CANCELLING` via `enqueue_write`) at [`api/routes.py:271`](api/routes.py:271).
  - `POST /backup/export`, `GET /backup/status`, `POST /backup/restore` — backup administration handlers are in [`api/routes.py:152`](api/routes.py:152).
  - `POST /jobs/{job_id}/resume` — resume handler loads `tools.{tool_name}.resume` and calls its `ResumeHandler` (if implemented) — handler code at [`api/routes.py:337`](api/routes.py:337).

- Tool interface (observed expectations):
  - Tools register themselves in `tools/registry.py` and may expose an `INPUT_MODEL` attribute for API-level validation. Evidence: `InputModel` handling in [`api/routes.py:71`](api/routes.py:71) and dynamic import at [`api/routes.py:74`](api/routes.py:74).
  - Tool results returned to the worker are normalized and may trigger callbacks (see `_do_callback_with_logging` in [`bot/engine/worker.py:37`](bot/engine/worker.py:37)).

- No CLI entrypoint or console tool is defined in packaging; primary run model is the FastAPI app (`app.py:1`](app.py:1)).

---

7. State, Persistence, and Data

- Database files and locations:
  - Main application DB: `data/sumanal.db` (path defined in [`database/connection.py:66`](database/connection.py:66)).
  - Logs DB: `data/logs.db` (path defined in [`database/connection.py:68`](database/connection.py:68)).

- Schema specifics (concrete column examples):
  - `jobs` DDL excerpt: `job_id`, `session_id`, `tool_name`, `args_json`, `status` with `CHECK(...)` including `'SKIPPED'` — see [`database/schemas/jobs.py:4`](database/schemas/jobs.py:4).
  - `job_items` DDL excerpt: `item_metadata` JSON, `status` column with `CHECK(... 'SKIPPED')`, `input_data`, `output_data` — see [`database/schemas/jobs.py:18`](database/schemas/jobs.py:18).

- Data formats & lifecycle:
  - Many fields store JSON-serialized strings (e.g., `args_json`, `item_metadata`, `payload_json` for logs). Call sites use `json.dumps`/`json.loads` consistently (see e.g. [`api/routes.py:132`](api/routes.py:132) and [`tools/scraper/task.py:181`](tools/scraper/task.py:181)).
  - WAL checkpointing is done periodically by the writer loop (see `PRAGMA wal_checkpoint(TRUNCATE)` at [`database/writer.py:128`](database/writer.py:128)).

- Migration / repair hooks:
  - The writer can attempt a best-effort table repair by requesting a repair script via `get_repair_script(table_name)` from `database/schemas` and running it; the reconciler can recreate missing tables. Evidence: `_attempt_table_repair` in [`database/writer.py:77`](database/writer.py:77) and `SchemaReconciler` in [`database/management/reconciler.py:59`](database/management/reconciler.py:59).

---

8. Dependencies & Integration

- Primary Python libraries used (from `requirements.txt` and usage evidence):
  - `fastapi` and `uvicorn` — used to serve the API (`api/routes.py:1`, `app.py:1`).
  - `pydantic` — request/response models in [`api/schemas.py:1`](api/schemas.py:1) and `InputModel` validation in [`api/routes.py:71`](api/routes.py:71).
  - `httpx` — used by `_do_callback_with_logging` to perform HTTP callbacks (`bot/engine/worker.py:128`](bot/engine/worker.py:128)).
  - `sqlite-vec` — optional native extension; referenced in [`database/connection.py:18`](database/connection.py:18) and guarded with `_VEC_PERMANENTLY_FAILED` (`database/connection.py:25`](database/connection.py:25)). If absent, system falls back to FTS5/keyword behaviors.
  - `openai>=1.47.0` — referenced in `requirements.txt` (`requirements.txt:49`](requirements.txt:49)) and the code uses structured-output calls with fallbacks to `json_object` (see `tools/scraper/extraction.py:368`](tools/scraper/extraction.py:368)).

- Coupling points & environment assumptions (observable):
  - The code assumes writable local disk (`data/` directory) for DBs. Evidence: `DB_PATH = Path("data") / "sumanal.db"` in [`database/connection.py:66`](database/connection.py:66).
  - Optional external systems: AnythingLLM callback endpoints rely on `config.ANYTHINGLLM_BASE_URL` and `config.ANYTHINGLLM_API_KEY` to be present for callback delivery (`bot/engine/worker.py:42`](bot/engine/worker.py:42)).

---

9. Setup, Build, and Execution (exact steps)

These steps reproduce how the repository is launched given the current files:

1. Create and activate Python virtualenv (Python 3.10+ is compatible with type usages seen). Install dependencies:

   - python -m venv .venv
   - .\.venv\Scripts\activate (Windows) or source .venv/bin/activate (Unix)
   - pip install -r [`requirements.txt:1`](requirements.txt:1)

2. Provide configuration values required by `config.py` (see uses in `bot/engine/worker.py:42`](bot/engine/worker.py:42) and other modules).

3. Initialize DB schema (the code offers schema DDL under `database/schemas/` and an initialization helper used at startup—see [`database/schemas/__init__.py:39`](database/schemas/__init__.py:39) for `get_init_script()` and [`utils/startup/database.py:1`](utils/startup/database.py:1) for startup wiring).

4. Start the API process: `uvicorn app:app --reload` or `python app.py` (the `app.py` module contains a FastAPI app and startup hooks at [`app.py:1`](app.py:1)).

5. Use `POST /tools/{tool_name}` to enqueue jobs, then observe worker activity and logs via `GET /jobs/{job_id}` (handlers in [`api/routes.py:48`](api/routes.py:48) and [`api/routes.py:213`](api/routes.py:213)).

---

10. Testing & Validation

- Tests present in the workspace (examples):
  - `tests/test_backup.py:1`](tests/test_backup.py:1) — unit tests validating backup schema and embedding byte-length invariants.
  - `tests/test_browser_e2e.py` (exists but not expanded here) — indicates an end-to-end browser exercise is included.

- How to run: `pytest` in repository root. Evidence: test file headers and standard pytest conventions (`tests/test_backup.py:3`](tests/test_backup.py:3)).

- Visible gaps in tests (observed):
  - No unit tests specifically asserting writer boundary/error handling are visible; writer behavior and race conditions are only covered indirectly if at all (see `database/writer.py` but no matching `tests/` files focused on the writer).

---

11. Known Limitations & Non-Goals (code-evident)

- Single-process design: not suitable for multi-node scaling due to reliance on local SQLite + in-process queues. Evidence: `queue.Queue`-based writer and local DB paths in [`database/writer.py:47`](database/writer.py:47) and [`database/connection.py:66`](database/connection.py:66).
- Log-dropping policy: logs writer drops entries when its bounded queue is full instead of back-pressuring application code (`database/logs_writer.py:116`](database/logs_writer.py:116)). That means logs may be lost under sustained high-throughput.
- HITL is blocking: the HITL path in the scraper blocks worker thread with `input()` and expects a local operator console as shown in [`tools/scraper/extraction.py:96`](tools/scraper/extraction.py:96). This is explicit and not asynchronous.
- Vector extension optionality: runtime behavior and performance vary depending on `sqlite-vec` availability; the code contains a permanent-failure flag to avoid repeated load attempts (see [`database/connection.py:25`](database/connection.py:25)).

---

12. Change Sensitivity (fragile parts — what to avoid changing lightly)

- Writer API shape and task tuple format: callers assume tasks are 3-tuples `(receipt, sql, params)` and special markers `EXEC_SCRIPT` / `TRANSACTION_MARKER`. Changing that shape breaks callers across the codebase (see normalization in `database/writer.py:145`](database/writer.py:145) and places where `enqueue_transaction` is used (e.g., [`tools/scraper/persistence.py:149`](tools/scraper/persistence.py:149)).
- Dual-logging contract: `dual_log` strictness (non-empty dict payload) is enforced and many modules depend on that; changing it requires updates across modules (see [`utils/logger/core.py:55`](utils/logger/core.py:55)).
- DB schema DDL centralization: `database/schemas/*` is used as the source of truth and the reconciler/writer rely on those scripts for repair and init. Modifying schemas must be coordinated with `database/management/reconciler.py` and `database/writer.py` repair hooks (see [`database/schemas/__init__.py:39`](database/schemas/__init__.py:39) and [`database/writer.py:77`](database/writer.py:77)).