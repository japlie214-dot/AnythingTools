Project: AnythingTools
=====================

This README documents the codebase as it exists now — only observable facts are stated, and every file reference links directly to a file path and anchor line to aid reconstruction.

Important: every referenced file is shown as a clickable pointer like [`path:line`](path:line). Use those links to inspect the exact implementation.

Primary entry points and files you should inspect first:
- [`app.py:1`](app.py:1)
- [`config.py:1`](config.py:1)
- [`api/routes.py:1`](api/routes.py:1)
- [`api/schemas.py:1`](api/schemas.py:1)
- [`bot/engine/worker.py:1`](bot/engine/worker.py:1)
- [`database/writer.py:1`](database/writer.py:1)
- [`database/logs_writer.py:1`](database/logs_writer.py:1)
- [`database/connection.py:1`](database/connection.py:1)
- [`tools/registry.py:1`](tools/registry.py:1)
- [`tools/base.py:14`](tools/base.py:14)
- [`tools/scraper/tool.py:1`](tools/scraper/tool.py:1)
- [`tools/scraper/task.py:1`](tools/scraper/task.py:1)
- [`tools/scraper/extraction.py:1`](tools/scraper/extraction.py:1)
- [`tools/scraper/resume.py:1`](tools/scraper/resume.py:1)
- [`utils/logger/formatters.py:1`](utils/logger/formatters.py:1)
- [`utils/browser_daemon.py:1`](utils/browser_daemon.py:1)
- [`utils/browser_lock.py:1`](utils/browser_lock.py:1)
- [`utils/startup/recovery.py:1`](utils/startup/recovery.py:1)

1. Project Overview
-------------------
Concrete operational summary (derived from code):
- This repository implements a single-process job hosting and execution service with an HTTP API. The API enqueues jobs into a local SQLite-backed `jobs` table and a local threaded worker manager polls and executes jobs in-process in dedicated threads. See [`api/routes.py:1`](api/routes.py:1) for how jobs are created and see the worker loop in [`bot/engine/worker.py:1`](bot/engine/worker.py:1).

- The most feature-complete tool is the scraper (browser-driven). The scraper driver is a managed Chrome-like `Driver` provided by a browser daemon abstraction. Look at [`utils/browser_daemon.py:1`](utils/browser_daemon.py:1) and the scraper's orchestration in [`tools/scraper/task.py:1`](tools/scraper/task.py:1).

- The system serializes all writes to the primary database through a single writer thread (`database/writer.py:1`) using a bounded queue; there is a separate high-throughput writer for structured logs (`database/logs_writer.py:1`). Files in [`database/schemas/`](database/schemas:1) contain table definitions and repair scripts referenced by the writer.

What it actually solves (observable behavior):
- Accepts HTTP job requests and persists them to SQLite; a local worker manager runs the job and records job status transitions, and can optionally send an HTTP callback with job results. See the enqueue path in [`api/routes.py:1`](api/routes.py:1) and the final callback code in [`bot/engine/worker.py:1`](bot/engine/worker.py:1).

- The codebase exposes an explicit resume mechanism: POST `/jobs/{job_id}/resume` is implemented (see [`api/routes.py:322`](api/routes.py:322)). The resume endpoint dynamically imports a tool-specific resume handler implemented in `tools/<tool>/resume.py` (for example [`tools/scraper/resume.py:1`](tools/scraper/resume.py:1)), calls its `ResumeHandler.check_resume_state()` and, when the handler reports `resumable`, re-queues the job (status `QUEUED`). The resume contract types (`ResumeReport` and `BaseResumeHandler`) are defined in [`tools/base.py:14`](tools/base.py:14) and the response model is `ResumeResponse` in [`api/schemas.py:32`](api/schemas.py:32).

- The scraper includes an explicit Human-in-the-Loop (HITL) design for ambiguous validation / paywall cases. The HITL flow is synchronous and console-blocking: the code writes `PAUSED_FOR_HITL` to the DB immediately before blocking on local stdin, attempts to flush the writer so external readers see the PAUSED state, and then updates the job status to `RUNNING` or `CANCELLING` after the operator responds. See [`tools/scraper/extraction.py:34`](tools/scraper/extraction.py:34) and the writer-flush usage at [`tools/scraper/extraction.py:69`](tools/scraper/extraction.py:69).

- Startup recovery implements a healing pass that downgrades jobs left in `RUNNING` or `PAUSED_FOR_HITL` to `INTERRUPTED` to avoid jobs stuck waiting for local console input after a restart. See [`utils/startup/recovery.py:17`](utils/startup/recovery.py:17).

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
  - [`api/routes.py:1`](api/routes.py:1) — HTTP endpoint handlers that validate input and call `enqueue_write` to persist job rows. Note the resume endpoint at [`api/routes.py:322`](api/routes.py:322).
  - [`api/schemas.py:1`](api/schemas.py:1) — Pydantic models used by routes, including `ResumeResponse` at [`api/schemas.py:32`](api/schemas.py:32).

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
  - `tools/base.py:14` defines the tool contract and the resume contract (`ResumeReport`, `BaseResumeHandler`).
  - For tools that implement resumption, a `resume.py` module exists (e.g. [`tools/scraper/resume.py:1`](tools/scraper/resume.py:1), [`tools/publisher/resume.py:1`](tools/publisher/resume.py:1)). Some tools (for example `batch_reader` and `draft_editor`) include `resume.py` stubs that explicitly return non-resumable reports; inspect their modules for exact behavior.
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

- Tool contract and resume contract
  - [`tools/base.py:14`](tools/base.py:14) defines `ToolResult`, `BaseTool.execute` semantics, and the resume types: `ResumeReport` and `BaseResumeHandler`. Per-tool resume handlers are located at `tools/<tool>/resume.py` and must implement `ResumeHandler.check_resume_state()` to return a `ResumeReport` used by the resume API at [`api/routes.py:322`](api/routes.py:322).

- Logging & Masking
  - The logging subsystem has two complementary strategies present in [`utils/logger/formatters.py:1`](utils/logger/formatters.py:1): historic regex-based redaction patterns and a newer semantic type wrapper family (`MaskableData`, `SensitiveVector`, `Base64Image`, etc.). The `FileFormatter` calls `_serialize_payload(...)` which now intercepts `MaskableData` instances and returns masked placeholders prior to JSON serialization.
    - Evidence: see the `MaskableData` and `Base64Image` classes in [`utils/logger/formatters.py:52`](utils/logger/formatters.py:52) and the `_serialize_payload` logic in the same file.
  - In short: large/binary payloads (images, embeddings) are wrapped at production sites (e.g., `utils/vision_utils.py`, `utils/vector_search.py`). The logger then masks them O(1) without expensive regex traversal.

- Vectors and `vec0` integration
  - Several virtual vec tables are defined under [`database/schemas/vector.py:1`](database/schemas/vector.py:1): `scraped_articles_vec`, `long_term_memories_vec`, `pdf_parsed_pages_vec`, all using `vec0(embedding float[1024])` if the extension is present.
  - Key invariants enforced by the code (evidence): rowids used in parent tables are deterministic positive 63-bit integers computed from ULIDs via a modulo mapping. See concrete rowid generation in [`tools/scraper/persistence.py:36`](tools/scraper/persistence.py:36), [`utils/vector_search.py:120`](utils/vector_search.py:120), and [`utils/pdf_utils.py:67`](utils/pdf_utils.py:67).
  - All current code paths that write to vec0 tables bundle operations into transaction bundles using `enqueue_transaction(...)` (writer-defined transaction marker). Where present, those bundles follow the pattern `DELETE FROM <vec_table> WHERE rowid = ?` then `INSERT INTO <vec_table> (rowid, embedding) VALUES (?, ?)` then `UPDATE parent SET embedding_status = 'EMBEDDED' ...`. See examples in [`tools/scraper/persistence.py:105`](tools/scraper/persistence.py:105) and similar files.

5. Detailed Behavior and Failure Modes
-------------------------------------
Normal run (explicit, by file):
- Client request accepted in [`api/routes.py:1`](api/routes.py:1) inserts a job (via `enqueue_write`) and returns an ID.
- [`database/writer.py:1`](database/writer.py:1) executes writes. It supports three task shapes: single `conn.execute(sql, params)`, `EXEC_SCRIPT` to run `executescript`, and `TRANSACTION_MARKER` (a list of `(stmt, binds)` executed in a BEGIN..COMMIT block). The writer increments a generation token on successful commits so long-lived read connections can refresh. See the writer loop in [`database/writer.py:1`](database/writer.py:1).

HITL and resume-specific behavior (concrete):
- When the scraper encounters ambiguous validation or payment-wall signals it calls the synchronous HITL helper in [`tools/scraper/extraction.py:34`](tools/scraper/extraction.py:34). The HITL function updates the DB to `PAUSED_FOR_HITL` then performs a blocking `input()` on the local console. It attempts to flush the writer with `wait_for_writes` before blocking so external processes (notably the resume API) observe the `PAUSED_FOR_HITL` state (`tools/scraper/extraction.py:69`).
- The resume API (`POST /jobs/{job_id}/resume`) rejects attempts to resume a job that is currently `PAUSED_FOR_HITL` with HTTP 409 to avoid conflicting with an operator who has an active console prompt (`api/routes.py:334`). The resume endpoint dynamically imports `tools.<tool>.resume` and calls `ResumeHandler.check_resume_state()`; if the handler reports `resumable` the job is moved to `QUEUED` and the manager is started (`api/routes.py:346`, `api/routes.py:356`). If the tool module is absent the endpoint returns 501 (`api/routes.py:350`); if the handler reports non-resumable a 400 is returned.

Edge cases / explicit failure behavior (code is explicit about these):
- Logs queue overflow: [`database/logs_writer.py:1`](database/logs_writer.py:1) will SIGTERM the process if the logs queue cannot be enqueued within a timeout — this is implemented verbatim in the file.
- Writer queue full: `enqueue_write(...)` logs a warning and drops non-critical writes (see [`database/writer.py:1`](database/writer.py:1)).
- Schema repair: on `no such table` errors the writer attempts to find a repair script in [`database/schemas/`](database/schemas:1) and executes it; if repair succeeds it retries the statement. See `_attempt_table_repair` in [`database/writer.py:42`](database/writer.py:42).
- Vector (`vec0`) rowid issues: the writer recognizes vec0-specific rowid error messages and logs them as warnings rather than crashing. See [`database/writer.py:38`](database/writer.py:38) for the detection predicate.

6. Public Interfaces (observable)
---------------------------------
HTTP endpoints (listed and precise):
- POST /api/tools/{tool_name} — [`api/routes.py:1`](api/routes.py:1). Input model is `JobCreateRequest` in [`api/schemas.py:1`](api/schemas.py:1). Output: job_id; side effect: enqueues a `jobs` INSERT.
- GET /api/jobs/{job_id} — [`api/routes.py:1`](api/routes.py:1) returns job status and recent logs (log retrieval queries are present in the same file).
- POST /jobs/{job_id}/resume — [`api/routes.py:322`](api/routes.py:322). Input: none beyond the job path parameter. Behavior: dynamically loads `tools.<tool>.resume.ResumeHandler`, calls `check_resume_state()`, and if the handler reports `resumable` transitions the job to `QUEUED` and returns a `ResumeResponse` model (`api/schemas.py:32`). If the job is `PAUSED_FOR_HITL` the endpoint returns 409; if the tool lacks a resume handler it returns 501; if the resume handler reports not resumable it returns 400.
- Backup endpoints: `/api/backup/export` and `/api/backup/restore` — implemented in [`api/routes.py:157`](api/routes.py:157) and dispatch into the backup runner under `database/backup/`.

Internal programmatic APIs (used by tools and the worker):
- `enqueue_write(sql, params)` and `enqueue_transaction(statements)` — schedule writes to the single-writer thread. See [`database/writer.py:141`](database/writer.py:141) and [`database/writer.py:218`](database/writer.py:218).
- `REGISTRY.create_tool_instance(name)` — instantiate a `BaseTool` subclass by name. See [`tools/registry.py:1`](tools/registry.py:1).

7. State, Persistence, and Data
--------------------------------
Databases used (explicit):
- Application DB: `data/sumanal.db` — configured and used by [`database/connection.py:1`](database/connection.py:1). Contains `jobs`, `scraped_articles`, `job_items`, `long_term_memories`, etc.
- Logs DB: `data/logs.db` — used exclusively for structured logs and drained on fatal failures. See [`database/logs_writer.py:1`](database/logs_writer.py:1).

State machine notes (evidence-backed):
- Job statuses used across the code include: `QUEUED`, `RUNNING`, `COMPLETED`, `PARTIAL`, `FAILED`, `CANCELLING`, `INTERRUPTED`, and `PAUSED_FOR_HITL`. The latter is used by the scraper's HITL code path to indicate a job is blocked waiting on a local operator (`tools/scraper/extraction.py:34`).
- Startup recovery downgrades `RUNNING` and `PAUSED_FOR_HITL` to `INTERRUPTED` during application initialization to avoid jobs lingering in console-blocked states across restarts (`utils/startup/recovery.py:17`).

Vector data lifecycle (explicit):
- Parent rows have a `vec_rowid` integer that is used as the rowid in the vec0 virtual table companion. The repo enforces a deterministic rowid mapping computed from ULIDs and bounded to 63 bits (see [`tools/scraper/persistence.py:36`](tools/scraper/persistence.py:36)).

8. Dependencies & Integration (enumerated, evidence-based)
--------------------------------------------------------
The codebase imports and uses the following third-party libraries (evidence = explicit import lines):
- `fastapi` / `uvicorn` — webserver and routing in [`app.py:1`](app.py:1) and [`api/routes.py:1`](api/routes.py:1).
- `httpx` — used for outbound callbacks in [`bot/engine/worker.py:1`](bot/engine/worker.py:1).
- `bs4` (`BeautifulSoup`) — scraper HTML extraction references in [`tools/scraper/extraction.py:5`](tools/scraper/extraction.py:5).
- `botasaurus` — a browser Driver abstraction used by the scraper (`from botasaurus.browser import Driver`) in [`tools/scraper/task.py:5`](tools/scraper/task.py:5).
- `psutil` — optional use in `utils/browser_daemon.py:1` / `utils/startup/cleanup.py:1` for process management.
- `python-dotenv` — loaded by [`config.py:1`](config.py:1).
- `openai` / LLM clients — used by the scraper summarization/validation logic (see [`tools/scraper/extraction.py:380`](tools/scraper/extraction.py:380)).
- `sqlite_vec` extension — optional extension usage detected in [`database/connection.py:1`](database/connection.py:1) (the connection code attempts to detect and adapt to a vector extension).

9. Setup, Build, and Execution (explicit steps)
-----------------------------------------------
Exact, code-derived steps to run from a clean environment:
1. Create a Python environment and install the packages listed in [`requirements.txt:1`](requirements.txt:1).
2. Set environment variables used by the code (defaults and names are declared in [`config.py:1`](config.py:1)). Minimal variables observed: `ANYTHINGLLM_BASE_URL`, `ANYTHINGLLM_API_KEY`, `CHROME_USER_DATA_DIR` (if using browser features), and typical FastAPI environment settings.
3. Start the HTTP server: `uvicorn app:app --reload --port 8000` (the `app` FastAPI instance is defined in [`app.py:1`](app.py:1)).
4. On startup, the app runs [`utils/startup/core.py:1`](utils/startup/core.py:1) steps which initialize the DB and may warm-up the browser daemon; the startup code may create `data/` and `artifacts/` directories automatically.

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
- The application writer drops non-critical writes when `write_queue` is full (`database/writer.py:1` and surrounding). This is an observable tradeoff (latency vs guaranteed persistence).
- The tool registry is conservative and not hot-reloadable; [`tools/registry.py:1`](tools/registry.py:1) contains gating semantics.
- Resumability: the repository now contains an explicit resume architecture. Per-tool resume handlers exist for several tools (see [`tools/scraper/resume.py:1`](tools/scraper/resume.py:1), [`tools/publisher/resume.py:1`](tools/publisher/resume.py:1)). Some tools intentionally opt-out by returning non-resumable reports in their `resume.py` stubs (for example `batch_reader` and `draft_editor`). The resume endpoint enforces a 409 when a job is `PAUSED_FOR_HITL` to avoid a race against a locally-blocked operator (`api/routes.py:334`).

12. Change Sensitivity (fragile areas) — Where to be careful
-----------------------------------------------------------
- Single-writer DB pattern: changing writer semantics (concurrency, transactional grouping) affects reader generation tokens and refresh logic in [`database/connection.py:1`](database/connection.py:1).
- Logging pipeline: the dual-path console + logs writer design is spread across [`utils/logger/core.py:1`](utils/logger/core.py:1), [`utils/logger/formatters.py:1`](utils/logger/formatters.py:1), and [`database/logs_writer.py:1`](database/logs_writer.py:1) and is sensitive to queue/backpressure changes.
- Browser lifecycle: centralization in [`utils/browser_daemon.py:1`](utils/browser_daemon.py:1) and the `browser_lock` concept in [`utils/browser_lock.py:1`](utils/browser_lock.py:1) means changing browser behavior touches many consumers (notably `tools/scraper/*`).
- HITL and resume: the synchronous, console-blocking HITL approach in [`tools/scraper/extraction.py:34`](tools/scraper/extraction.py:34) is deliberately simple but creates operational constraints (single-operator model, reliance on writer flush semantics). Changes to the writer-flush/visibility guarantees or to the HITL implementation will require careful end-to-end validation.