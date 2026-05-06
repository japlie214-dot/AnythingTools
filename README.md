Project: AnythingTools
=====================

This README documents the codebase as it exists now — only observable facts are stated, and every file reference links directly to a file path and anchor line to aid reconstruction.

Important: every referenced file is shown as a clickable pointer like [`path:line`](path:line). Use those links to inspect the exact implementation.

Primary entry points and files you should inspect first:
- [`app.py:1`](app.py:1)
- [`config.py:1`](config.py:1)
- [`api/routes.py:1`](api/routes.py:1)
- [`bot/engine/worker.py:1`](bot/engine/worker.py:1)
- [`database/writer.py:1`](database/writer.py:1)
- [`database/logs_writer.py:1`](database/logs_writer.py:1)
- [`database/connection.py:1`](database/connection.py:1)
- [`tools/registry.py:1`](tools/registry.py:1)
- [`tools/scraper/tool.py:1`](tools/scraper/tool.py:1)
- [`tools/scraper/task.py:1`](tools/scraper/task.py:1)
- [`tools/scraper/extraction.py:1`](tools/scraper/extraction.py:1)
- [`tools/scraper/persistence.py:1`](tools/scraper/persistence.py:1)
- [`utils/logger/formatters.py:1`](utils/logger/formatters.py:1)
- [`utils/browser_daemon.py:1`](utils/browser_daemon.py:1)
- [`utils/browser_lock.py:1`](utils/browser_lock.py:1)

1. Project Overview
-------------------
Concrete operational summary (derived from code):
- This repository implements a single-process job hosting and execution service with an HTTP API. The API enqueues jobs into a local SQLite-backed `jobs` table and a local threaded worker manager polls and executes jobs in-process in dedicated threads. See [`api/routes.py:1`](api/routes.py:1) for how jobs are created and see the worker loop in [`bot/engine/worker.py:1`](bot/engine/worker.py:1).

- The most feature-complete tool is the scraper (browser-driven). The scraper driver is a managed Chrome-like `Driver` provided by a browser daemon abstraction. Look at [`utils/browser_daemon.py:1`](utils/browser_daemon.py:1) and the scraper's orchestration in [`tools/scraper/task.py:1`](tools/scraper/task.py:1).

- The system serializes all writes to the primary database through a single writer thread (`database/writer.py:1`) using a bounded queue; there is a separate high-throughput writer for structured logs (`database/logs_writer.py:1`). Files in [`database/schemas/`](database/schemas:1) contain table definitions and repair scripts referenced by the writer.

What it actually solves (observable behavior):
- Accepts HTTP job requests and persists them to SQLite; a local worker manager runs the job and records job status transitions, and can optionally send an HTTP callback with job results. See the enqueue path in [`api/routes.py:1`](api/routes.py:1) and the final callback code in [`bot/engine/worker.py:1`](bot/engine/worker.py:1).

What it explicitly does NOT do (evidence):
- There is no distributed coordination or clustered worker logic — the worker manager polls a local SQLite `jobs` table and spawns threads within the same process. See [`bot/engine/worker.py:1`](bot/engine/worker.py:1) and [`database/connection.py:1`](database/connection.py:1).
- There is no built-in cloud object store integration. Artifact writes are local filesystem operations under `artifacts/` described in [`tools/scraper/tool.py:1`](tools/scraper/tool.py:1).

2. High-Level Architecture
--------------------------
Major components (file-rooted responsibilities):
- HTTP server & routes: [`app.py:1`](app.py:1) and [`api/routes.py:1`](api/routes.py:1).
- Tool discovery/registry: [`tools/registry.py:1`](tools/registry.py:1) — a conservative whitelist-style loader.
- Worker/runner: [`bot/engine/worker.py:1`](bot/engine/worker.py:1) (poller and lifecycle state machine) + [`bot/engine/tool_runner.py:1`](bot/engine/tool_runner.py:1) for safe invocation.
- Tool implementations: `tools/` (most code lives in `tools/scraper/*`). The scraper uses a browser driver and extraction helpers in [`tools/scraper/extraction.py:1`](tools/scraper/extraction.py:1).
- Persistence writers: [`database/writer.py:1`](database/writer.py:1) for application writes; [`database/logs_writer.py:1`](database/logs_writer.py:1) for structured logs.
- Browser process manager: [`utils/browser_daemon.py:1`](utils/browser_daemon.py:1) and cross-thread `browser_lock` in [`utils/browser_lock.py:1`](utils/browser_lock.py:1).

Data and control flow (concrete step-by-step, with exact code locations):
1. Client -> POST /api/tools/{tool_name} handled by [`api/routes.py:1`](api/routes.py:1). Input is validated (pydantic models in [`api/schemas.py:1`](api/schemas.py:1)) and an `INSERT` to `jobs` is enqueued with `enqueue_write(...)`.
2. `enqueue_write(...)` places a tuple on the `write_queue` consumed by `db_writer_worker()` in [`database/writer.py:1`](database/writer.py:1). Writes are executed sequentially on a single writer connection.
3. `UnifiedWorkerManager` in [`bot/engine/worker.py:1`](bot/engine/worker.py:1) polls `jobs`, claims work, enqueues status transitions, and spawns a thread to run the tool via `run_tool_safely`/`BaseTool.execute`.
4. For browser-bound tools (e.g., the scraper), code obtains a `Driver` via [`utils/browser_daemon.py:1`](utils/browser_daemon.py:1) and acquires [`utils/browser_lock.py:1`](utils/browser_lock.py:1) for mutual exclusion for the duration of the `run()`.
5. Tool results are normalized, written back to `jobs.result_json`, and an outbound callback may be made using `httpx` per [`bot/engine/worker.py:1`](bot/engine/worker.py:1).

Execution model: local, threaded, event-driven around changes in the `jobs` table; startup/shutdown lifecycle is coordinated in [`app.py:1`](app.py:1).

3. Repository Structure (walkthrough)
-------------------------------------
Top-level directories and their observed roles (each statement is verifiable by reading the referenced file):

- [`api/`](api:1)
  - [`api/routes.py:1`](api/routes.py:1) — HTTP endpoint handlers that validate input and call `enqueue_write` to persist job rows.
  - [`api/schemas.py:1`](api/schemas.py:1) — Pydantic models used by routes.

- [`bot/`](bot:1)
  - [`bot/engine/worker.py:1`](bot/engine/worker.py:1) — Worker manager and job lifecycle poller/claim/execution loop. Look for `_run_loop` and `_run_job` for exact semantics.
  - [`bot/engine/tool_runner.py:1`](bot/engine/tool_runner.py:1) — central place that executes a tool and normalizes exceptions.

- [`database/`](database:1)
  - [`database/connection.py:1`](database/connection.py:1) — read/write connection creation; manages a generation token used to refresh long-lived read connections.
  - [`database/writer.py:1`](database/writer.py:1) — single writer thread; supports `enqueue_transaction` (transaction bundles) and `EXEC_SCRIPT` markers. It also contains repair logic (`_attempt_table_repair`) that executes SQL from [`database/schemas/`](database/schemas:1) when `no such table` errors occur.
  - [`database/logs_writer.py:1`](database/logs_writer.py:1) — separate queue/worker for log writes. Important: that worker treats queue overflow as fatal (SIGTERM) — see the code for the explicit check and behavior.
  - [`database/schemas/`](database/schemas:1) — SQL DDL for tables including `vec0` virtual tables and FTS tables. See [`database/schemas/vector.py:1`](database/schemas/vector.py:1) for vector-related table definitions and triggers.

- [`tools/`](tools:1)
  - Contains plugin-style tools. `tools/registry.py:1` registers a small set of whitelisted tools. Tools present include `scraper`, `draft_editor`, `publisher`, `batch_reader`.
  - `tools/scraper/` is the most complete example and contains the orchestration of scraping, LLM calls, embedding generation, and persistence. Key files: [`tools/scraper/task.py:1`](tools/scraper/task.py:1), [`tools/scraper/extraction.py:1`](tools/scraper/extraction.py:1), [`tools/scraper/persistence.py:1`](tools/scraper/persistence.py:1), prompt files in `tools/scraper/`.

- [`utils/`](utils:1)
  - Helpers used across the system: logging (`utils/logger/*`), browser daemon (`utils/browser_daemon.py:1`), `utils/browser_lock.py:1`, embedding helpers (`utils/vector_search.py:1`), pdf helpers (`utils/pdf_utils.py:1`), startup orchestration (`utils/startup/*`).
  - [`utils/logger/formatters.py:1`](utils/logger/formatters.py:1) contains serialization, masking, and formatters for logs. It now contains a `MaskableData` semantic wrapper family used for upstream masking of large/binary payloads (see section "Core Concepts").

- [`deprecated/`](deprecated:1)
  - A large set of legacy modules remain; they are not imported by the main runtime. Their presence is direct evidence of prior refactors or replacements.

- [`tests/`](tests:1)
  - Minimal test surface. See [`tests/test_backup.py:1`](tests/test_backup.py:1) and [`tests/test_browser_e2e.py:1`](tests/test_browser_e2e.py:1).

4. Core Concepts & Domain Model
------------------------------
Key domain artifacts and invariants visible in code:

- Jobs and job_items
  - `jobs` table rows model enqueued units of work. See SQL usage in [`api/routes.py:1`](api/routes.py:1) and schema in [`database/schemas/jobs.py:1`](database/schemas/jobs.py:1).
  - `job_items` support fine-grained step tracking for tools like the scraper (look at queries in [`tools/scraper/task.py:1`](tools/scraper/task.py:1)).

- Tool contract
  - [`tools/base.py:1`](tools/base.py:1) defines `ToolResult` and `BaseTool.execute` semantics. The worker uses `REGISTRY.create_tool_instance` to instantiate tools (see [`tools/registry.py:1`](tools/registry.py:1)).

- Logging & Masking
  - The logging subsystem has two complementary strategies present in [`utils/logger/formatters.py:1`](utils/logger/formatters.py:1): historic regex-based redaction patterns (`_REDACT_B64_PATTERN` and `_REDACT_PEEK_PATTERN`) and a newer semantic type wrapper family (`MaskableData`, `SensitiveVector`, `Base64Image`, etc.). The `FileFormatter` calls `_serialize_payload(...)` which now intercepts `MaskableData` instances and returns masked placeholders prior to JSON serialization.
    - Evidence: see the `MaskableData` and `Base64Image` classes in [`utils/logger/formatters.py:52`](utils/logger/formatters.py:52) and the `_serialize_payload` logic in the same file.
  - In short: large/binary payloads (images, embeddings) are wrapped at production sites (e.g., [`utils/vision_utils.py:1`](utils/vision_utils.py:1) wraps base64 screenshots with `Base64Image`; [`utils/vector_search.py:1`](utils/vector_search.py:1) wraps embedding lists with `SensitiveVector`). The logger then masks them O(1) without expensive regex traversal.

- Vectors and `vec0` integration
  - Several virtual vec tables are defined under [`database/schemas/vector.py:1`](database/schemas/vector.py:1): `scraped_articles_vec`, `long_term_memories_vec`, `pdf_parsed_pages_vec`, all using `vec0(embedding float[1024])` if the extension is present.
  - Key invariants enforced by the code (evidence): rowids used in parent tables (`scraped_articles.vec_rowid`, `long_term_memories.id`, `pdf_parsed_pages.id`) are deterministic positive 63-bit integers computed from ULIDs via a modulo mapping. See concrete rowid generation in [`tools/scraper/persistence.py:36`](tools/scraper/persistence.py:36), [`utils/vector_search.py:120`](utils/vector_search.py:120), and [`utils/pdf_utils.py:67`](utils/pdf_utils.py:67): the code computes `vec_rowid = (_raw % 0x7FFFFFFFFFFFFFFE) + 1` (numeric constant `0x7FFFFFFFFFFFFFFE = 9223372036854775806`). This guarantees values are in a strictly-positive range less than SQLite signed integer max.
  - All current code paths that write to vec0 tables bundle operations into transaction bundles using `enqueue_transaction(...)` (writer-defined transaction marker). Where present, those bundles follow the pattern `DELETE FROM <vec_table> WHERE rowid = ?` then `INSERT INTO <vec_table> (rowid, embedding) VALUES (?, ?)` then `UPDATE parent SET embedding_status = 'EMBEDDED' ...`. See examples in [`tools/scraper/persistence.py:105`](tools/scraper/persistence.py:105), [`tools/scraper/task.py:217`](tools/scraper/task.py:217), [`utils/vector_search.py:126`](utils/vector_search.py:126), and [`utils/pdf_utils.py:72`](utils/pdf_utils.py:72).

5. Detailed Behavior and Failure Modes
-------------------------------------
Normal run (explicit, by file):
- Client request accepted in [`api/routes.py:1`](api/routes.py:1) inserts a job (via `enqueue_write`) and returns an ID.
- `database/writer.py:1` executes writes. It supports three task shapes: single `conn.execute(sql, params)`, `EXEC_SCRIPT` to run `executescript`, and `TRANSACTION_MARKER` (a list of `(stmt, binds)` executed in a BEGIN..COMMIT block). The writer increments a generation token (`_write_generation`) on successful commits so long-lived read connections can refresh. See the writer loop in [`database/writer.py:1`](database/writer.py:1).

Edge cases / explicit failure behavior (code is explicit about these):
- Logs queue overflow: [`database/logs_writer.py:1`](database/logs_writer.py:1) will SIGTERM the process if the logs queue cannot be enqueued within a timeout — this is implemented verbatim in the file.
- Writer queue full: `enqueue_write(...)` logs a warning and drops non-critical writes (see [`database/writer.py:1`](database/writer.py:1)).
- Schema repair: on `no such table` errors the writer attempts to find a repair script in [`database/schemas/`](database/schemas:1) and executes it; if repair succeeds it retries the statement. See `_attempt_table_repair` in [`database/writer.py:42`](database/writer.py:42).
- Vector (`vec0`) rowid issues: the writer recognizes vec0-specific rowid error messages (`_is_vec0_rowid_error`) and logs them as warnings rather than crashing. See [`database/writer.py:38`](database/writer.py:38) for the detection predicate.

6. Public Interfaces (observable)
---------------------------------
HTTP endpoints (listed and precise):
- POST /api/tools/{tool_name} — [`api/routes.py:1`](api/routes.py:1). Input model is `JobCreateRequest` in [`api/schemas.py:1`](api/schemas.py:1). Output: job_id; side effect: enqueues a `jobs` INSERT.
- GET /api/jobs/{job_id} — [`api/routes.py:1`](api/routes.py:1) returns job status and recent logs (log retrieval queries are present in the same file).
- Backup endpoints: `/api/backup/export` and `/api/backup/restore` — implemented in [`api/routes.py:1`](api/routes.py:1) and dispatch into the backup runner under `database/backup/`.

Internal programmatic APIs (used by tools and the worker):
- `enqueue_write(sql, params)` and `enqueue_transaction(statements)` — schedule writes to the single-writer thread. See [`database/writer.py:141`](database/writer.py:141) and [`database/writer.py:218`](database/writer.py:218).
- `REGISTRY.create_tool_instance(name)` — instantiate a `BaseTool` subclass by name. See [`tools/registry.py:1`](tools/registry.py:1).

7. State, Persistence, and Data
--------------------------------
Databases used (explicit):
- Application DB: `data/sumanal.db` — configured and used by [`database/connection.py:1`](database/connection.py:1). Contains `jobs`, `scraped_articles`, `job_items`, `long_term_memories`, etc.
- Logs DB: `data/logs.db` — used exclusively for structured logs and drained on fatal failures. See [`database/logs_writer.py:1`](database/logs_writer.py:1).

Vector data lifecycle (explicit):
- Parent rows have a `vec_rowid` integer that is used as the rowid in the vec0 virtual table companion. The repo enforces a deterministic rowid mapping computed from ULIDs and bounded to 63 bits: `vec_rowid = (_id_raw % 0x7FFFFFFFFFFFFFFE) + 1` (see [`tools/scraper/persistence.py:36`](tools/scraper/persistence.py:36), [`utils/vector_search.py:120`](utils/vector_search.py:120), [`utils/pdf_utils.py:67`](utils/pdf_utils.py:67)).
- Writes to vec0 tables are always performed as an atomic `DELETE` then `INSERT` inside a `TRANSACTION_MARKER`/`enqueue_transaction` bundle to avoid sqlite-vec internal initialization races; see the concrete bundles in [`tools/scraper/persistence.py:105`](tools/scraper/persistence.py:105) and similar files.

Cleanup and triggers:
- The SQL triggers in [`database/schemas/vector.py:50`](database/schemas/vector.py:50) remove vec table rows when parent `scraped_articles` rows are deleted or when `vec_rowid` changes. The triggers are defined to maintain vec/table lifecycle.

8. Dependencies & Integration (enumerated, evidence-based)
--------------------------------------------------------
The codebase imports and uses the following third-party libraries (evidence = explicit import lines):
- `fastapi` / `uvicorn` — webserver and routing in [`app.py:1`](app.py:1) and [`api/routes.py:1`](api/routes.py:1).
- `httpx` — used for outbound callbacks in [`bot/engine/worker.py:1`](bot/engine/worker.py:1).
- `bs4` (`BeautifulSoup`) — scraper HTML extraction references in [`tools/scraper/extraction.py:5`](tools/scraper/extraction.py:5).
- `botasaurus` — a browser Driver abstraction used by the scraper (`from botasaurus.browser import Driver`) in [`tools/scraper/task.py:10`](tools/scraper/task.py:10).
- `psutil` — optional use in `utils/browser_daemon.py:1` / `utils/startup/cleanup.py:1` for process management.
- `python-dotenv` — loaded by [`config.py:1`](config.py:1).
- `openai` — LLM client interaction error handling appears in [`tools/scraper/extraction.py:276`](tools/scraper/extraction.py:276).
- `sqlite_vec` extension — optional extension usage detected in [`database/connection.py:1`](database/connection.py:1) (the connection code attempts to detect and adapt to a vector extension).

9. Setup, Build, and Execution (explicit steps)
-----------------------------------------------
Exact, code-derived steps to run from a clean environment:
1. Create a Python environment and install the packages listed in [`requirements.txt:1`](requirements.txt:1) (the repo imports above show additional necessary packages).
2. Set environment variables used by the code (defaults and names are declared in [`config.py:1`](config.py:1)). Minimal variables observed: `ANYTHINGLLM_BASE_URL`, `ANYTHINGLLM_API_KEY`, `CHROME_USER_DATA_DIR` (if using browser features), and typical FastAPI environment settings.
3. Start the HTTP server: `uvicorn app:app --reload --port 8000` (the `app` FastAPI instance is defined in [`app.py:1`](app.py:1)).
4. On startup, the app runs `utils/startup/core.py` steps which initialize the DB and optionally warm-up the browser daemon; the startup code will create `data/` and `artifacts/` directories automatically.

Platform constraints visible in code:
- Browser tooling expects a Chrome-like binary and a user data dir path (`CHROME_USER_DATA_DIR`) — startup/browser warmup imposes timeouts.
- The logs writer's fatal behavior on overflow means running with sufficiently large `logs` queue or a supervised environment is recommended.

10. Testing & Validation
------------------------
Existing tests (explicit):
- [`tests/test_backup.py:1`](tests/test_backup.py:1)
- [`tests/test_browser_e2e.py:1`](tests/test_browser_e2e.py:1)

How to run tests: `pytest` in repo root. Tests are limited in scope: they touch backup and an E2E browser scenario. No comprehensive unit test coverage for many critical subsystems (writer, registry, logger, worker) is visible in the `tests/` directory.

11. Known Limitations & Non-Goals (explicit evidence)
----------------------------------------------------
- Logs queue overflow kills the process (`database/logs_writer.py:1`). This is observable and explicit.
- The application writer drops non-critical writes when `write_queue` is full (`database/writer.py:169` and surrounding). This is an observable tradeoff (latency vs guaranteed persistence).
- The tool registry is conservative and not hot-reloadable; `tools/registry.py:1` contains `_loaded` gate semantics.
- Resumability in the scraper appears scaffolded: there are helper functions and `job_items` usage, but current code paths suggest the resumability shim is not fully active in all code paths (see resume checks and comments in [`tools/scraper/task.py:120`](tools/scraper/task.py:120) and the presence of no-op `_check_step`-style shims referenced in comments).

12. Change Sensitivity (fragile areas) — Where to be careful
-----------------------------------------------------------
- Single-writer DB pattern: changing writer semantics (concurrency, transactional grouping) affects reader generation tokens and refresh logic in [`database/connection.py:1`](database/connection.py:1).
- Logging pipeline: the dual-path console + logs writer design is spread across `utils/logger/core.py:1`, `utils/logger/formatters.py:1`, and `database/logs_writer.py:1` and is sensitive to queue/backpressure changes.
- Browser lifecycle: centralization in [`utils/browser_daemon.py:1`](utils/browser_daemon.py:1) and the `browser_lock` concept in [`utils/browser_lock.py:1`](utils/browser_lock.py:1) means changing browser behavior touches many consumers (notably `tools/scraper/*`).