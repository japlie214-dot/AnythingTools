Project: AnythingTools
======================================

This README is an evidence-first, line-referenced, and reproducible description of the repository as it exists now. Every factual claim below is supported by code, filenames, comments, or directory structure present in the repository. Where the code is ambiguous, that ambiguity is called out explicitly.

File reference convention used below: every file is referenced as a clickable pointer such as [`path:line`](path:line) to allow direct inspection of the exact implementation.

IMPORTANT: This README describes the code "as it is now". Do not treat any sentence as a description of future intent or goals. If the code contains vestigial or deprecated files, that is documented and flagged as such.

Contents
--------
1. Project Overview
2. High-Level Architecture
3. Repository Structure (top-level walkthrough)
4. Core Concepts & Domain Model
5. Detailed Behavior (step-by-step with edge cases)
6. Public Interfaces (HTTP & programmatic)
7. State, Persistence, and Data
8. Dependencies & Integration (evidence-based)
9. Setup, Build, and Execution (exact steps)
10. Testing & Validation
11. Known Limitations & Non-Goals
12. Change Sensitivity (fragile boundaries)
13. Changes (Evolutionary Analysis from Current Code)


1. Project Overview
-------------------
What this system actually does (concrete, observable):

- Hosts a local HTTP API for queuing and managing "jobs" and runs those jobs in-process using a local worker manager. Evidence: [`app.py:1`](app.py:1), [`api/routes.py:1`](api/routes.py:1), and the worker loop in [`bot/engine/worker.py:1`](bot/engine/worker.py:1).

- Persists application-level state to a local SQLite database accessed by a single writer thread. Evidence: [`database/writer.py:1`](database/writer.py:1) (single writer loop), and connection code in [`database/connection.py:1`](database/connection.py:1).

- Implements a plugin-style tool system; the most complete tool is a browser-driven scraper that uses a managed Chrome driver (Botasaurus). Evidence: [`tools/registry.py:1`](tools/registry.py:1), scraper code at [`tools/scraper/task.py:1`](tools/scraper/task.py:1) and browser interfaces in [`utils/browser_daemon.py:1`](utils/browser_daemon.py:1).

- Exposes an explicit resume API for jobs that can be resumed after interruption. Evidence: resume endpoint in [`api/routes.py:322`](api/routes.py:322) and the resume contract types in [`tools/base.py:14`](tools/base.py:14), plus per-tool resume handlers such as [`tools/scraper/resume.py:1`](tools/scraper/resume.py:1).

What it actually solves (operationally):

- Accepts job submissions (via HTTP), records the job in the `jobs` table, runs the job locally, and writes job results back to the DB. Evidence: request handling in [`api/routes.py:1`](api/routes.py:1), queuing through `enqueue_write(...)` in [`database/writer.py:141`](database/writer.py:141), and job processing in [`bot/engine/worker.py:1`](bot/engine/worker.py:1).

What it explicitly does NOT do (evidence):

- Not a distributed job cluster — the work queue is a local SQLite table and the worker manager runs in-process. Evidence: worker poller and DB usage in [`bot/engine/worker.py:1`](bot/engine/worker.py:1) and [`database/connection.py:1`](database/connection.py:1).

- No cloud object storage integration is present in the codebase. Evidence: artifact code writes to local filesystem locations referenced in tools, e.g., [`tools/scraper/tool.py:1`](tools/scraper/tool.py:1).


2. High-Level Architecture
--------------------------
Major components (file-rooted responsibilities):

- HTTP Server & Routing: [`app.py:1`](app.py:1), [`api/routes.py:1`](api/routes.py:1).
- Tool Registry & Discovery: [`tools/registry.py:1`](tools/registry.py:1).
- Worker Manager & Runner: [`bot/engine/worker.py:1`](bot/engine/worker.py:1) (polling/claiming) and [`bot/engine/tool_runner.py:1`](bot/engine/tool_runner.py:1) (safe execution wrapper).
- Single-writer DB subsystem: [`database/writer.py:1`](database/writer.py:1) and connection helpers in [`database/connection.py:1`](database/connection.py:1).
- Tools: directory `tools/` with tool-specific modules, most notably `tools/scraper/`.
- Browser lifecycle & automation helpers: [`utils/browser_daemon.py:1`](utils/browser_daemon.py:1), [`utils/browser_lock.py:1`](utils/browser_lock.py:1), and DOM/SoM helpers in [`utils/som_utils.py:1`](utils/som_utils.py:1).

Data & control flow (concrete sequence with file references):

1. Client submits job -> route in [`api/routes.py:1`](api/routes.py:1) validates using Pydantic models (see [`api/schemas.py:1`](api/schemas.py:1)) and enqueues SQL writes via `enqueue_write(...)` called by the routes.
2. `enqueue_write(...)` places write operations on a queue consumed by the writer thread in [`database/writer.py:1`](database/writer.py:1), which serializes all mutations to the application database.
3. Worker manager (`bot/engine/worker.py:1`) polls the `jobs` table, claims work, then runs the tool using `REGISTRY.create_tool_instance(name)` and the `tool_runner` wrapper in [`bot/engine/tool_runner.py:1`](bot/engine/tool_runner.py:1).
4. Browser-based tools obtain a managed driver from the daemon (`utils/browser_daemon.py:104`), acquire the cross-thread browser lock (`utils/browser_lock.py:1`), and perform navigation and DOM extraction using SoM utilities (`utils/som_utils.py:1`) and scraper helpers (`tools/scraper/extraction.py:1`).
5. Results are written back into the DB using `enqueue_write(...)`, and outbound callbacks (HTTP) are optionally issued from the worker (`bot/engine/worker.py:1`).

Runtime model: single-process, multi-threaded, event-driven off DB state transitions, orchestrated by startup code in [`app.py:1`](app.py:1) and `utils/startup/*` modules.


3. Repository Structure (top-level walkthrough)
-----------------------------------------------
The top-level layout (every item below is present in the repository and referenced by code):

- [`app.py`](app.py:1) — FastAPI app bootstrap. See startup event hooks and server initialization.
- [`config.py`](config.py:1) — Environment-variable-driven configuration with documented defaults. Key variables referenced in code: `CHROME_USER_DATA_DIR`, `ANYTHINGLLM_*`, `JOB_WATCH_INTERVAL_SECONDS`. See [`config.py:20`](config.py:20) and the AnythingLLM defaults at [`config.py:65`](config.py:65).
- [`api/`](api:1) — HTTP endpoints and Pydantic schemas. Important: [`api/routes.py:1`](api/routes.py:1) contains endpoints for job creation, job status, resume (`/jobs/{job_id}/resume` at [`api/routes.py:322`](api/routes.py:322)), and backup endpoints at [`api/routes.py:157`](api/routes.py:157).
- [`bot/`](bot:1) — runtime worker & tool execution code. Key: worker poller in [`bot/engine/worker.py:1`](bot/engine/worker.py:1), safe execution wrapper in [`bot/engine/tool_runner.py:1`](bot/engine/tool_runner.py:1).
- [`clients/`](clients:1) — client adapters, including LLM provider wiring in [`clients/llm/providers/azure.py:1`](clients/llm/providers/azure.py:1).
- [`database/`](database:1) — single-writer, logs writer, and schema files (DDL) in [`database/schemas/`](database/schemas:1). The writer has explicit repair logic and transactional bundling; inspect [`database/writer.py:1`](database/writer.py:1).
- [`tools/`](tools:1) — tool plugins. `tools/registry.py:1` registers whitelisted tools. Notable directories: `tools/scraper/` (full browser-driven pipeline), `tools/publisher/`, `tools/draft_editor/`, `tools/batch_reader/`.
- [`utils/`](utils:1) — cross-cutting helpers: logging (`utils/logger/*`), browser management (`utils/browser_daemon.py:1`), SoM utilities (`utils/som_utils.py:1`), startup orchestration (`utils/startup/*`).
- [`deprecated/`](deprecated:1) — many former modules retained for reference; not imported by the main runtime. The presence of this directory is direct evidence of prior refactoring.
- [`tests/`](tests:1) — minimal tests; see [`tests/test_backup.py:1`](tests/test_backup.py:1) and [`tests/test_browser_e2e.py:1`](tests/test_browser_e2e.py:1).

Notes on non-obvious structure:
- The repository keeps a `deprecated/` directory with many modules that are not imported by runtime code. Their presence is evidence that older patterns existed and were intentionally kept for reference rather than removed (see `deprecated/bot/core/*` and `deprecated/tools/*`).


4. Core Concepts & Domain Model
------------------------------
Terminology and concrete invariants discovered in code:

- Jobs and job_items: The primary unit of work is a `jobs` table row that contains status, result JSON, and metadata. `job_items` exist for fine-grained tracking; SQL references are visible in `tools/scraper/task.py:1` and `database/schemas/jobs.py:1`.

- Tool contract: `tools/base.py:14` defines the `BaseTool`/`ToolResult` and the resume contract (`ResumeReport`, `BaseResumeHandler`) used by the resume API. Tools that support resumption publish a `resume.py` module and implement `ResumeHandler.check_resume_state()`.

- Single-writer invariant: All application writes go through a single writer thread consumed from a queue (`database/writer.py:1`). The writer implements three shapes for writes: single statement, `EXEC_SCRIPT` (executescript), and `TRANSACTION_MARKER` (bundles statements into a transaction). The writer also implements schema repair on "no such table" errors using scripts in [`database/schemas/`](database/schemas:1).

- Logging & masking: The logging subsystem implements structured logs and a maskable payload concept: see `MaskableData`/`Base64Image` wrappers in [`utils/logger/formatters.py:52`](utils/logger/formatters.py:52) and serialization logic in the same module.

- Browser/SoM model: The scraper uses a managed Botasaurus `Driver` and a Set-of-Marks (SoM) approach to inject `data-ai-id` into the DOM, remove overlays and extract readable HTML. Evidence: [`utils/som_utils.py:1`](utils/som_utils.py:1), `inject_som()` and `wait_for_dom_stability()` functions.


5. Detailed Behavior (normal execution + edge cases)
---------------------------------------------------
Normal processing flow (from client request to job completion):

1. HTTP POST to create a job: handled in [`api/routes.py:1`](api/routes.py:1) — input validated with Pydantic models in [`api/schemas.py:1`](api/schemas.py:1). The route schedules DB writes by calling `enqueue_write(...)` to the writer queue.
2. Writer thread processes the queued SQL operations and commits them to the application DB. The writer increments a generation token so that long-lived read connections can refresh after a commit; see [`database/writer.py:1`](database/writer.py:1) and [`database/connection.py:1`](database/connection.py:1).
3. The worker poller (`bot/engine/worker.py:1`) observes the queued job, claims it, and calls into `REGISTRY.create_tool_instance(name)` (see [`tools/registry.py:1`](tools/registry.py:1)). `tool_runner` executes `BaseTool.execute` with safe exception handling.
4. For the scraper tool specifically:
   - A `Driver` is obtained from the browser daemon: see [`utils/browser_daemon.py:104`](utils/browser_daemon.py:104). Note: the driver is constructed with `wait_for_complete_page_load=False` to adopt an eager DOM-ready strategy; inspect [`utils/browser_daemon.py:117`](utils/browser_daemon.py:117).
   - Navigation is performed via `safe_google_get(...)` which now uses a thread-based timeout guard to avoid indefinite TTFB hangs. Evidence: [`utils/browser_utils.py:12`](utils/browser_utils.py:12) (thread-based guard, 45s timeout, monotonic timing markers, and best-effort page abort via `window.stop()`).
   - DOM stabilization is enforced using `wait_for_dom_stability(...)` in [`utils/som_utils.py:40`](utils/som_utils.py:40), which considers `document.readyState` values of both `"interactive"` and `"complete"`.
   - The scraper injects SoM markers (`inject_som(...)`) and extracts readable HTML via `extract_hybrid_html(...)`.
   - If a page fails validation or is paywalled, the scraper triggers a synchronous human-in-the-loop (HITL) prompt. The code updates DB row status to `PAUSED_FOR_HITL` and blocks on a console `input()` while holding no external locks. Evidence: [`tools/scraper/extraction.py:34`](tools/scraper/extraction.py:34) and the writer flush call at [`tools/scraper/extraction.py:69`](tools/scraper/extraction.py:69).
   - After human resolution on HITL `proceed`, the code re-extracts the live DOM state (no programmatic re-navigation) and overwrites `raw_html`, `b64_image`, and `slim_sum` prior to summarisation. Evidence: changes in [`tools/scraper/extraction.py:303`](tools/scraper/extraction.py:303) and [`tools/scraper/extraction.py:352`](tools/scraper/extraction.py:352).

Edge cases and explicit failure modes (code is explicit):

- Logs queue overflow is fatal: the logs writer will kill the process if the logs queue cannot be enqueued (see the explicit check and behavior in [`database/logs_writer.py:1`](database/logs_writer.py:1)).
- Writer queue overflow results in dropped non-critical writes (see `enqueue_write(...)` behavior in [`database/writer.py:1`](database/writer.py:1)).
- The writer attempts to repair missing tables by selecting a script from [`database/schemas/`](database/schemas:1) when encountering `no such table`—the repair path is explicit in [`database/writer.py:42`](database/writer.py:42).
- Browser/CDP and SoM injection errors are handled explicitly; the warmup sequence will perform a surgical kill on detected infinite JS loops during SoM injection (see [`utils/browser_daemon.py:218`](utils/browser_daemon.py:218) and the `MarkingError` handling in [`utils/browser_daemon.py:220`](utils/browser_daemon.py:220)).


6. Public Interfaces
--------------------
HTTP endpoints (observable and exact):

- POST /api/tools/{tool_name} — create a job. Implementation: [`api/routes.py:1`](api/routes.py:1) (input type `JobCreateRequest` in [`api/schemas.py:1`](api/schemas.py:1)). Side effect: enqueues a `jobs` INSERT.

- GET /api/jobs/{job_id} — retrieve job status and recent logs. See [`api/routes.py:1`](api/routes.py:1).

- POST /jobs/{job_id}/resume — resume a job; dynamically loads `tools.<tool>.resume.ResumeHandler` and calls `check_resume_state()`; various HTTP semantics: 409 if `PAUSED_FOR_HITL`, 501 if resume support absent, 400 if not resumable (see [`api/routes.py:322`](api/routes.py:322), [`api/routes.py:350`](api/routes.py:350), and [`api/routes.py:346`](api/routes.py:346)).

- Backup APIs (export/restore) under `/api/backup/*` implemented in [`api/routes.py:157`](api/routes.py:157) and backed by `database/backup/` runner code.

Programmatic APIs available for internal use (evidence-based function names):

- `enqueue_write(sql, params)` and `enqueue_transaction(statements)` — scheduling writes to the writer thread. See [`database/writer.py:141`](database/writer.py:141) and [`database/writer.py:218`](database/writer.py:218).
- `REGISTRY.create_tool_instance(name)` — instantiate tools per `tools/registry.py:1`.


7. State, Persistence, and Data
--------------------------------
Databases and file-backed storage (explicit):

- Application DB: `data/sumanal.db` (connection referenced in [`database/connection.py:1`](database/connection.py:1)).
- Logs DB: `data/logs.db` used by the logs writer (`database/logs_writer.py:1`).
- Artifacts: files (screenshots, archived HTML) are written locally under configured artifact paths referenced in tools (e.g., `tools/scraper/persistence.py:1`).

Vector workflows and invariants:

- The repo contains vec0 virtual-table DDL under [`database/schemas/vector.py:1`](database/schemas/vector.py:1) and writes to vec tables are bundled into transactions. Deterministic rowid generation used for vec0 mapping is implemented in [`tools/scraper/persistence.py:36`](tools/scraper/persistence.py:36) and `utils/vector_search.py:120`.

State transitions and statuses: `QUEUED`, `RUNNING`, `COMPLETED`, `PARTIAL`, `FAILED`, `CANCELLING`, `INTERRUPTED`, and `PAUSED_FOR_HITL` are present and used across the code; evidence spans `api/routes.py`, `database/writer.py`, and `tools/scraper/extraction.py`.

Startup recovery: `utils/startup/recovery.py:17` will downgrade `RUNNING` and `PAUSED_FOR_HITL` to `INTERRUPTED` on restart to avoid jobs stuck waiting on console input.


8. Dependencies & Integration (evidence-based)
---------------------------------------------
Third-party libraries explicitly imported in the code:

- `fastapi`, `uvicorn`: webserver and routing. Evidence: [`app.py:1`](app.py:1) and [`api/routes.py:1`](api/routes.py:1).
- `httpx`: used for outbound callbacks in the worker. Evidence: [`bot/engine/worker.py:1`](bot/engine/worker.py:1).
- `bs4` (BeautifulSoup): HTML parsing in the scraper. Evidence: [`tools/scraper/extraction.py:5`](tools/scraper/extraction.py:5).
- `botasaurus`: Botasaurus `Driver` is used for browser automation by the scraper. Evidence: imports like `from botasaurus.browser import Driver` in [`utils/browser_daemon.py:16`](utils/browser_daemon.py:16) and `tools/scraper/task.py:5`.
- `psutil`: optional process management used in `utils/browser_daemon.py:11`.
- `dotenv` (`python-dotenv`): used by [`config.py:4`](config.py:4) to load environment variables.
- `openai` / LLM packages / any provider wrappers: used by the scraper summarisation calls (see [`tools/scraper/extraction.py:380`](tools/scraper/extraction.py:380) and `clients/llm/providers/*`).
- Optional SQLite vector extension: connection code detects and adapts to `vec0` when available (`database/connection.py:1`).

Assumptions and environment coupling visible in code:

- Browser automation expects a Chrome-like binary and a persistent user-data directory, referenced by `CHROME_USER_DATA_DIR` in [`config.py:21`](config.py:21).
- The logs writer fatal behavior requires deployment that can handle a process kill on log overflow (see [`database/logs_writer.py:1`](database/logs_writer.py:1)).


9. Setup, Build, and Execution (exact steps derived from code)
------------------------------------------------------------
From a clean environment, the repository can be run with the following code-derived steps:

1. Create a Python environment and install dependencies: `pip install -r requirements.txt` (see [`requirements.txt:1`](requirements.txt:1)).
2. Ensure environment variables used by `config.py` are set. The code references variables such as `ANYTHINGLLM_BASE_URL`, `ANYTHINGLLM_API_KEY`, `CHROME_USER_DATA_DIR`, `API_KEY`. See [`config.py:8`](config.py:8), [`config.py:20`](config.py:20), and `ANYTHINGLLM_*` defaults in [`config.py:65`](config.py:65).
3. Start the HTTP server: `uvicorn app:app --reload --port 8000` (the FastAPI application is defined in [`app.py:1`](app.py:1)).
4. On startup, the app runs startup scripts under `utils/startup/` to initialize DB and optionally warm up the browser daemon (`utils/startup/core.py:1`). The startup path may create `data/` and `artifacts/` directories.

Platform constraints observed in code: Windows-first operational hints appear in comments (for example in `utils/browser_daemon.py`) and the code avoids Unix-only timeouts in navigation guards (see [`utils/browser_utils.py:12`](utils/browser_utils.py:12) using `concurrent.futures` instead of `signal`).


10. Testing & Validation
------------------------
Tests present (explicit):

- `tests/test_backup.py` — covers backup/restore behavior. See [`tests/test_backup.py:1`](tests/test_backup.py:1).
- `tests/test_browser_e2e.py` — end-to-end browser scenario. See [`tests/test_browser_e2e.py:1`](tests/test_browser_e2e.py:1).

How to run tests: `pytest` in the repo root. The tests that exist are limited in scope and do not provide exhaustive coverage for writer, registry, or logging pipeline.

Coverage gaps visible from the repo:
- No comprehensive unit tests for `database/writer.py` or `bot/engine` subsystems are present in `tests/`.
- No CI scripts found in the top-level to automate tests; no Dockerfile present.


11. Known Limitations & Non-Goals (explicit, code-backed)
--------------------------------------------------------
- Logs queue overflow triggers a process kill (visible in [`database/logs_writer.py:1`](database/logs_writer.py:1)). This is a deliberate, explicit behavior.
- Non-critical writes are dropped when the writer queue is full; see `enqueue_write(...)` in [`database/writer.py:1`](database/writer.py:1).
- HITL model is synchronous and console-blocking: there is a single-operator assumption visible in `tools/scraper/extraction.py:34` and in the resume endpoint behavior which returns 409 for `PAUSED_FOR_HITL` (`api/routes.py:334`).
- The tool registry is intentionally conservative and not hot-reloadable. Evidence: `tools/registry.py:1` implements a whitelist loader.


12. Change Sensitivity (fragile areas)
--------------------------------------
Parts of the system most likely to require coordinated changes:

- Single-writer DB pattern: changing `database/writer.py:1` semantics (e.g., parallel writes) would affect `database/connection.py:1` generation tokens, the reader refresh logic, and transaction bundle semantics in tools.
- Logging pipeline: changes to payload masking and formatters touch `utils/logger/formatters.py:1`, `utils/logger/core.py:1`, and the logs writer (`database/logs_writer.py:1`); the serialization expects `MaskableData` wrappers in some call sites.
- Browser lifecycle and SoM: `utils/browser_daemon.py:104` centralizes the Driver and warmup, `utils/som_utils.py:1` carries DOM-stability and SoM logic; modifications here ripple into `tools/scraper/*`.
- HITL/resume interactions: the resume API behavior and startup recovery rely on writer visibility of `PAUSED_FOR_HITL` and the writer flush semantics. Altering writer flush/visibility or HITL patterns will require end-to-end validation.