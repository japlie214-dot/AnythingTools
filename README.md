# AnythingTools — Precise Codebase Snapshot

This README is a factual, evidence-backed description of the repository as it exists in this workspace. Every claim is directly traceable to repository artifacts (files, constants, DDL, code paths). Statements that infer historical change are explicitly labeled with an evidence list and a confidence level.

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

- Concrete observed behavior:
  - The repository exposes an HTTP API that enqueues tool-run jobs and persists them to a local SQLite database. Evidence: enqueue + route handler at [`api/routes.py:48`](api/routes.py:48) and the enqueue write at [`api/routes.py:131`](api/routes.py:131).
  - A background worker manager polls persisted jobs and executes tool implementations in per-job threads. Evidence: polling and thread spawn in [`bot/engine/worker.py:177`](bot/engine/worker.py:177) and [`bot/engine/worker.py:334`](bot/engine/worker.py:334).
  - A single dedicated writer thread serializes main-database commits; the system exposes a `WriteReceipt` primitive for callers to wait for durability. Evidence: writer loop and `WriteReceipt` in [`database/writer.py:25`](database/writer.py:25) and writer loop start at [`database/writer.py:355`](database/writer.py:355).
  - The system implements a unified write pipeline for scraped articles that concurrently streams data to PyArrow Parquet files and enqueues SQLite transactions. Evidence: [`database/articles/writer.py:1`](database/articles/writer.py:1) and [`database/articles/parquet_stream.py:1`](database/articles/parquet_stream.py:1).

- Purpose (observable from code): provide a single-process, durable job orchestration runtime that runs registered tools, persists job and item state, and separates log persistence from the main application write path. Evidence: job DDL in [`database/schemas/jobs.py:3`](database/schemas/jobs.py:3), logs writer in [`database/logs_writer.py:14`](database/logs_writer.py:14), and tool registry in [`tools/registry.py:1`](tools/registry.py:1).

- Explicit non-goals (observable absences): no distributed broker, no multi-node coordination. Evidence: local DB path in [`database/connection.py:66`](database/connection.py:66) and in-process queues in [`database/writer.py:47`](database/writer.py:47).

---

2. High-Level Architecture

- Primary runtime components and direct file pointers:
  - HTTP API and request validation: [`api/routes.py:1`](api/routes.py:1), [`api/schemas.py:1`](api/schemas.py:1).
  - Worker manager and per-job execution: [`bot/engine/worker.py:177`](bot/engine/worker.py:177), [`bot/engine/tool_runner.py:1`](bot/engine/tool_runner.py:1).
  - Tool registration and discovery: [`tools/registry.py:1`](tools/registry.py:1).
  - Main DB connection manager and optional vector extension loader: [`database/connection.py:75`](database/connection.py:75).
  - Single-writer loop and transaction API: [`database/writer.py:108`](database/writer.py:108).
  - Separate logs writer and bounded-queue policy: [`database/logs_writer.py:14`](database/logs_writer.py:14).
  - Unified Article Write Pipeline: [`database/articles/writer.py:1`](database/articles/writer.py:1), [`database/articles/parquet_stream.py:1`](database/articles/parquet_stream.py:1).
  - Example tool family (scraper): [`tools/scraper/task.py:24`](tools/scraper/task.py:24), [`tools/scraper/persistence.py:1`](tools/scraper/persistence.py:1).

- Concrete dataflow (traceable steps):
  1) Client POST /tools/{tool_name} -> handler validates and enqueues a `jobs` row via `enqueue_write(...)`. Evidence: [`api/routes.py:48`](api/routes.py:48), validation at [`api/routes.py:71`](api/routes.py:71), write at [`api/routes.py:131`](api/routes.py:131).
  2) `UnifiedWorkerManager` polls `jobs` for `QUEUED`/`INTERRUPTED` and marks selected jobs `RUNNING` with `enqueue_write(...)`. Evidence: poll SQL at [`bot/engine/worker.py:214`](bot/engine/worker.py:214) and marking at [`bot/engine/worker.py:256`](bot/engine/worker.py:256).
  3) Worker spawns per-job thread; tool instantiated via registry and executed via `run_tool_safely` / `asyncio.run`. Evidence: registry usage [`bot/engine/worker.py:325`](bot/engine/worker.py:325) and execution at [`bot/engine/worker.py:334`](bot/engine/worker.py:334).
  4) Tools persist results. Specifically, the scraper uses `enqueue_article_write` which streams to Parquet and enqueues a DB transaction. Evidence: [`tools/scraper/persistence.py:54`](tools/scraper/persistence.py:54) and [`database/articles/writer.py:1`](database/articles/writer.py:1).
  5) Writer thread commits transactions serially and increments a generation counter used to refresh read connections. Evidence: generation increment at [`database/writer.py:155`](database/writer.py:155) and connection refresh in [`database/connection.py:87`](database/connection.py:87).

- Concurrency model (code-observed): single process, multi-threaded. Evidence: thread creation in [`bot/engine/worker.py:193`](bot/engine/worker.py:193) and writer thread in [`database/writer.py:355`](database/writer.py:355).

---

3. Repository Structure (top-level walkthrough)

Below are top-level artifacts that materially affect runtime, with exact entry pointers (line numbers included):

- [`app.py:1`](app.py:1)  FastAPI app; main process entrypoint and lifecycle wiring.
- [`config.py:1`](config.py:1)  central configuration constants referenced across modules.
- [`requirements.txt:1`](requirements.txt:1)  pinned dependency list used at install time.

Key directories and their primary runtime roles (one example pointer each):
- [`api/`]: HTTP endpoints and validation (see [`api/routes.py:1`](api/routes.py:1)).
- [`bot/`]: worker lifecycle and tool execution (see [`bot/engine/worker.py:177`](bot/engine/worker.py:177)).
- [`database/`]: connection, writer, logs writer, schema DDL (see [`database/writer.py:108`](database/writer.py:108) and [`database/schemas/jobs.py:3`](database/schemas/jobs.py:3)).
- [`database/articles/`]: Unified Parquet + SQLite write pipeline for articles (see [`database/articles/writer.py:1`](database/articles/writer.py:1)).
- [`tools/`]: tool registration and concrete tool implementations (see [`tools/registry.py:1`](tools/registry.py:1) and `tools/scraper/*`).
- [`utils/`]: helper subsystems (logging, browser daemon, startup wiring). Evidence: [`utils/logger/core.py:27`](utils/logger/core.py:27), [`utils/browser_daemon.py:1`](utils/browser_daemon.py:1), [`utils/startup/database.py:1`](utils/startup/database.py:1).
- [`deprecated/`]: archived/legacy implementations (see many files under [`deprecated/`](deprecated/)).

---

4. Core Concepts & Domain Model (observed)

- Jobs and job_items (canonical persisted models):
  - `jobs` table fields include `job_id`, `session_id`, `tool_name`, `args_json`, `status` with an explicit `CHECK(...)` on `status`. Evidence: DDL in [`database/schemas/jobs.py:3`](database/schemas/jobs.py:3).
  - `job_items` contains `item_metadata` (JSON), `status`, `input_data`, `output_data`. Evidence: DDL at [`database/schemas/jobs.py:18`](database/schemas/jobs.py:18).

- Persistence primitives and invariants:
  - Single-writer invariant: all main-DB commits are performed by the writer thread via `enqueue_*` APIs. Evidence: `enqueue_transaction(...)` usage and writer loop in [`database/writer.py:329`](database/writer.py:329) and callers in tools.
  - WriteReceipt contract: callers optionally wait for durable confirmation via `receipt.wait(...)`. Evidence: `WriteReceipt` definition and usage in [`database/writer.py:25`](database/writer.py:25) and [`tools/scraper/persistence.py:149`](tools/scraper/persistence.py:149).
  - Unified Article Write: Articles are written to Parquet (via `StreamingParquetWriter`) and SQLite (via `enqueue_transaction`) atomically. Parquet writes use `.tmp` files and are committed upon successful DB transaction. Evidence: [`database/articles/writer.py:1`](database/articles/writer.py:1).

- Logging contract:
  - `SumAnalLogger.dual_log` requires a non-empty dict `payload` and enqueues log writes to the separate logs writer. Evidence: payload check at [`utils/logger/core.py:55`](utils/logger/core.py:55) and logs enqueue at [`utils/logger/core.py:128`](utils/logger/core.py:128).
  - Strict Tagging: All log tags must follow the `Category:Sub-Category:Action` format (exactly 3 parts). Evidence: usage in `tools/scraper/tool.py` and `utils/logger/structured.py`.

- Vector/embedding handling:
  - Embedding storage and related DDL exist; usage is conditional on `sqlite_vec` availability. Evidence: vector DDL in [`database/schemas/vector.py:1`](database/schemas/vector.py:1) and vec-load logic in [`database/connection.py:27`](database/connection.py:27).

---

5. Detailed Behavior

Normal execution (concrete, observable steps):
1. POST /tools/{tool_name} validated by API; writes a `jobs` row via `enqueue_write(...)`. Evidence: [`api/routes.py:48`](api/routes.py:48), write at [`api/routes.py:131`](api/routes.py:131).
2. `UnifiedWorkerManager` polls `jobs` and marks chosen jobs `RUNNING`. Evidence: poll at [`bot/engine/worker.py:214`](bot/engine/worker.py:214) and mark at [`bot/engine/worker.py:256`](bot/engine/worker.py:256).
3. Worker spawns a thread and calls `run_tool_safely`/`asyncio.run(...)`. Evidence: [`bot/engine/worker.py:334`](bot/engine/worker.py:334).
4. Tools use persistence helpers. The scraper uses `enqueue_article_write` to stream to Parquet and enqueue a DB transaction. Evidence: [`tools/scraper/persistence.py:54`](tools/scraper/persistence.py:54).
5. Writer executes transactions, increments generation, and performs WAL checkpointing. Evidence: writer generation increment at [`database/writer.py:155`](database/writer.py:155) and WAL checkpoint at [`database/writer.py:128`](database/writer.py:128).

Edge cases and failure handling (observed):
- Write queue overflow: `enqueue_*` uses `put_nowait` and will reject if the queue is full. Evidence: overflow handling at [`database/writer.py:293`](database/writer.py:293).
- No-such-table repair: writer recognizes "no such table" and can attempt to apply a repair script sourced from schema modules. Evidence: error detection and repair at [`database/writer.py:64`](database/writer.py:64) and repair call at [`database/writer.py:77`](database/writer.py:77).
- Vector extension errors: writer contains special handling for vec-related errors and records `DB:Writer:VecError` logs. Evidence: vec error detection functions at [`database/writer.py:72`](database/writer.py:72) and handling at [`database/writer.py:208`](database/writer.py:208).
- Human-in-the-loop (HITL) path is blocking: scraper code performs synchronous `input()` and updates job status to `PAUSED_FOR_HITL`. Evidence: blocking HITL in [`tools/scraper/extraction.py:42`](tools/scraper/extraction.py:42) and pause behavior at [`tools/scraper/extraction.py:72`](tools/scraper/extraction.py:72).
- Parquet Write Failure: If a Parquet write fails, the system logs a `Backup:Storage:Rollback` error but proceeds with the DB write. If the DB write fails, the system attempts to unlink the `.tmp.parquet` fragment for that specific article. Evidence: [`database/articles/writer.py:1`](database/articles/writer.py:1).

---

6. Public Interfaces

- HTTP endpoints (exact code locations):
  - `POST /tools/{tool_name}`  handler and enqueue in [`api/routes.py:48`](api/routes.py:48) and [`api/routes.py:131`](api/routes.py:131).
  - `GET /jobs/{job_id}`  status retrieval in [`api/routes.py:213`](api/routes.py:213).
  - `DELETE /jobs/{job_id}`  cancellation flow in [`api/routes.py:271`](api/routes.py:271).
  - Backup endpoints (`POST /backup/export`, etc.) in [`api/routes.py:152`](api/routes.py:152).
  - `POST /jobs/{job_id}/resume`  resume handler that loads `tools.{tool_name}.resume` and calls `ResumeHandler` if present (see [`api/routes.py:337`](api/routes.py:337)).

- Tool interface (observable contract):
  - Tools register via registry and may expose `INPUT_MODEL` for API-level validation. Evidence: registry in [`tools/registry.py:1`](tools/registry.py:1) and validation branch in [`api/routes.py:71`](api/routes.py:71).
  - Tools that need to persist results are expected to use the writer APIs (`enqueue_transaction`, `enqueue_write`) to guarantee durability.

---

7. State, Persistence, and Data

- Database locations (concrete values):
  - Main DB path: `data/sumanal.db` defined at [`database/connection.py:66`](database/connection.py:66).
  - Logs DB path: `data/logs.db` defined at [`database/connection.py:68`](database/connection.py:68).

- Representative schema details (exact DDL references):
  - `jobs` and `job_items` DDL: [`database/schemas/jobs.py:3`](database/schemas/jobs.py:3) and [`database/schemas/jobs.py:18`](database/schemas/jobs.py:18).
  - Vector-related schema: [`database/schemas/vector.py:1`](database/schemas/vector.py:1).
  - Scraped Articles schema: [`database/articles/schema.py:1`](database/articles/schema.py:1).

- Data formats and lifecycle:
  - Several columns store JSON-serialized blobs (evidence: `args_json` and `item_metadata` usage in [`api/routes.py:132`](api/routes.py:132) and [`tools/scraper/task.py:181`](tools/scraper/task.py:181)).
  - Articles are backed up to Parquet files in 5-minute buckets. Evidence: [`database/articles/parquet_stream.py:26`](database/articles/parquet_stream.py:26).
  - WAL checkpointing and periodic maintenance are executed by the writer (`PRAGMA wal_checkpoint(TRUNCATE)` at [`database/writer.py:128`](database/writer.py:128)).

---

8. Dependencies & Integration

- Key libraries (from `requirements.txt` and code usage):
  - `fastapi` / `uvicorn`  API server. Evidence: app module in [`app.py:1`](app.py:1) and router in [`api/routes.py:1`](api/routes.py:1).
  - `pydantic`  input/output models in [`api/schemas.py:1`](api/schemas.py:1).
  - `httpx`  used for callbacks in [`bot/engine/worker.py:128`](bot/engine/worker.py:128).
  - `pyarrow` / `pandas`  used for Parquet streaming and backup. Evidence: [`database/articles/parquet_stream.py:1`](database/articles/parquet_stream.py:1).
  - `sqlite-vec` (optional native extension) referenced in [`database/connection.py:18`](database/connection.py:18) and guarded by `_VEC_PERMANENTLY_FAILED` at [`database/connection.py:25`](database/connection.py:25).
  - `openai>=1.47.0` appears in [`requirements.txt:49`](requirements.txt:49) and structured-output handling appears in [`tools/scraper/extraction.py:368`](tools/scraper/extraction.py:368).

- Integration assumptions (observable):
  - Local writable disk for `data/` is required (see `DB_PATH` in [`database/connection.py:66`](database/connection.py:66)).
  - Optional external callbacks rely on `config.ANYTHINGLLM_BASE_URL` and `config.ANYTHINGLLM_API_KEY` usage in worker callbacks (`bot/engine/worker.py:42`](bot/engine/worker.py:42)).

---

9. Setup, Build, and Execution (exact steps)

Reproduce the runtime given repository files:
1. Create a Python virtualenv and install dependencies:
   - python -m venv .venv
   - .\.venv\Scripts\activate (Windows) or source .venv/bin/activate (Unix)
   - pip install -r [`requirements.txt:1`](requirements.txt:1)
2. Provide configuration values referenced by `config.py` (used in multiple modules; see usages in [`bot/engine/worker.py:42`](bot/engine/worker.py:42)).
3. Initialize DB schema using DDL in [`database/schemas/__init__.py:39`](database/schemas/__init__.py:39) or allow startup wiring to create missing tables (see [`utils/startup/database.py:1`](utils/startup/database.py:1)).
4. Start the API process: `uvicorn app:app --reload` or `python app.py` (FastAPI app at [`app.py:1`](app.py:1)).
5. Enqueue jobs via `POST /tools/{tool_name}` and monitor with `GET /jobs/{job_id}` (handlers in [`api/routes.py:48`](api/routes.py:48) and [`api/routes.py:213`](api/routes.py:213)).

---

10. Testing & Validation

- Tests present (direct file references):
  - Unit tests: [`tests/test_backup.py:1`](tests/test_backup.py:1) validates backup DDL and embedding byte-length invariants.
  - Browser E2E: [`tests/test_browser_e2e.py:1`](tests/test_browser_e2e.py:1) indicates an end-to-end browser scenario exists.

- How to run tests: `pytest` in repository root (standard pytest layout; test files under `tests/`).

- Observed test coverage gaps: no explicit unit tests for writer concurrency/race behavior were found.

---

11. Known Limitations & Non-Goals (code-evident)

- Single-process SQLite-based architecture prevents safe multi-node scaling. Evidence: local DB path and in-process queue usage (`database/connection.py:66`](database/connection.py:66) and `database/writer.py:47`](database/writer.py:47)).
- Logs can be dropped if the logs-writer queue is saturated; the code intentionally drops entries rather than back-pressuring application write paths. Evidence: drop counter and bounded queue in [`database/logs_writer.py:116`](database/logs_writer.py:116).
- Human-in-the-loop flows block worker threads using synchronous console input. Evidence: `input()`-based HITL in [`tools/scraper/extraction.py:42`](tools/scraper/extraction.py:42).
- Runtime behavior depends on optional native `sqlite-vec`; the code records a permanent failure flag to avoid retrying extension load. Evidence: `_VEC_PERMANENTLY_FAILED` in [`database/connection.py:25`](database/connection.py:25).

---

12. Change Sensitivity (fragile parts to avoid changing without coordinated updates)

- Writer task tuple and marker format: many callers expect the exact task shape (receipt, sql, params) and use `EXEC_SCRIPT` / `TRANSACTION_MARKER` markers. Evidence: normalization in [`database/writer.py:145`](database/writer.py:145) and callers in `tools/*`.
- `SumAnalLogger.dual_log` payload contract: modules rely on `dual_log` requiring a non-empty dict (change would cascade). Evidence: payload check at [`utils/logger/core.py:55`](utils/logger/core.py:55) and widespread calls to `dual_log`.
- Schema DDL centralization: `database/schemas/*` is treated as the source-of-truth and reconciler/writer use those scripts for repairs. Evidence: `get_init_script()` at [`database/schemas/__init__.py:39`](database/schemas/__init__.py:39) and reconciler code at [`database/management/reconciler.py:59`](database/management/reconciler.py:59).