# AnythingTools — Precise Code-First README (Evidence-Only)

This README is an evidence-first, code-grounded description of the repository state as it exists in the workspace. Every assertion below is supported by a direct citation to one or more files present in the repository; each file reference is shown as a clickable link to its location in the working tree.

IMPORTANT: This document intentionally avoids speculation. When a conclusion is inferred rather than directly visible in code, the inference is labeled with a confidence level and justification.

---

1.  PROJECT OVERVIEW

- Operational summary (concrete): AnythingTools is a Python backend that exposes a FastAPI HTTP surface and executes registered "tools" as jobs. The HTTP entrypoint is [`app.py`](app.py:1) which creates a `FastAPI` instance and runs a startup lifespan that initializes writers and other subsystems.

- What the system actually does (evidence):
  - Accepts job requests via the REST endpoint implemented in [`api/routes.py`](api/routes.py:1) (see the `POST /api/tools/{tool_name}` handler at [`api/routes.py:51`](api/routes.py:51)).
  - Persists job records to a primary SQLite database (main DB path configured in [`database/connection.py`](database/connection.py:25)).
  - Runs a background poller/worker (`UnifiedWorkerManager`) implemented in [`bot/engine/worker.py`](bot/engine/worker.py:177) to claim and execute queued jobs.
  - Executes tool implementations located under [`tools/`](tools/) (example: the `scraper` tool in [`tools/scraper/tool.py`](tools/scraper/tool.py:1)).
  - Maintains a high-throughput structured logging pipeline that writes to a dedicated logs database (`data/logs.db`) using a specialized logs writer in [`database/logs_writer.py`](database/logs_writer.py:1) and the logs schema in [`database/schemas/logs.py`](database/schemas/logs.py:3).

- What it explicitly does NOT do (observable):
  - There is no frontend/UI code in the repository; the entrypoint is a server: see [`app.py`](app.py:1).
  - The codebase uses a single-writer SQLite architecture (writer threads in [`database/writer.py`](database/writer.py:228) and [`database/logs_writer.py`](database/logs_writer.py:117)), so it does not implement a horizontally-scaled multi-node writer.
  - Real-time WebSocket streaming is not present in the code paths for job results; job status is exposed via the polling API implemented in [`api/routes.py`](api/routes.py:223).


2.  HIGH-LEVEL ARCHITECTURE

- Major runtime components (with evidence):
  - HTTP/API layer: [`app.py`](app.py:1) + router definitions in [`api/routes.py`](api/routes.py:1).
  - Main operational DB and write layer: `DatabaseManager` and the single-writer thread in [`database/connection.py`](database/connection.py:33) and [`database/writer.py`](database/writer.py:228).
  - Dedicated logs DB and write layer: `LogsDatabaseManager` and the logs writer in [`database/connection.py`](database/connection.py:118) and [`database/logs_writer.py`](database/logs_writer.py:58).
  - Worker engine: `UnifiedWorkerManager` in [`bot/engine/worker.py`](bot/engine/worker.py:177).
  - Tool execution wrapper: [`bot/engine/tool_runner.py`](bot/engine/tool_runner.py:22) which centralizes error handling.
  - Tool modules (examples): [`tools/scraper/tool.py`](tools/scraper/tool.py:28) and its helpers in [`tools/scraper/extraction.py`](tools/scraper/extraction.py:1).
  - Structured dual-logger API: `utils.logger` package; primary implementation in [`utils/logger/core.py`](utils/logger/core.py:27) and contract in [`utils/logger/__init__.py`](utils/logger/__init__.py:4).
  - Browser lifecycle manager (for browser-bound tools): `ChromeDaemonManager` in [`utils/browser_daemon.py`](utils/browser_daemon.py:32).

- Data flow (concrete, step-by-step as observed in code):
  1. Client requests `POST /api/tools/{tool}` handled by [`api/routes.py`](api/routes.py:51).
  2. API persists a `jobs` record to the main DB via `enqueue_write(...)` implemented in [`database/writer.py`](database/writer.py:141) (call sites: [`api/routes.py:138`](api/routes.py:138)).
  3. `UnifiedWorkerManager` polls `jobs` table using `DatabaseManager.get_read_connection()` (`[`database/connection.py`](database/connection.py:37)) and selects pending/queued jobs (`[`bot/engine/worker.py`](bot/engine/worker.py:212)).
  4. Worker spawns the tool execution via `ToolRunner`/`run_tool_safely` (`[`bot/engine/tool_runner.py`](bot/engine/tool_runner.py:22)).
  5. Tools log via the dual logger API `log.dual_log(...)` (`[`utils/logger/core.py`](utils/logger/core.py:43)); structured log records are enqueued to the logs writer (`logs_enqueue_write(...)`) for persistence into `logs.db` (`[`database/logs_writer.py`](database/logs_writer.py:101)).
  6. Tool outputs and job final payloads are written to the main DB via `enqueue_write(...)` (`[`database/writer.py`](database/writer.py:141)).
  7. Optional callback steps post results to external services using call-and-log patterns found in [`bot/engine/worker.py::_do_callback_with_logging`](bot/engine/worker.py:37).

- Execution model: event-driven job execution implemented as a poller + worker threads; database writes go through single-writer threads for serializability. See polling loop in [`bot/engine/worker.py`](bot/engine/worker.py:202) and writer thread logic in [`database/writer.py`](database/writer.py:66).


3.  REPOSITORY STRUCTURE (top-level walkthrough)

Below are the top-level directories and the primary files we used as evidence for this README (every file referenced is clickable):

- [`app.py`](app.py:1) — application entrypoint; creates `FastAPI` app and lifespan hooks (startup/shutdown). The documented run command appears at the top comment in [`app.py:8`](app.py:8).

- [`api/`](api/): API router and pydantic models. Primary evidence: [`api/routes.py`](api/routes.py:1).
  - [`api/routes.py`](api/routes.py:1) — all REST endpoints; job enqueue and job status endpoints are implemented here (see `POST /api/tools/{tool_name}` at [`api/routes.py:51`](api/routes.py:51)).

- [`bot/`](bot/): worker and orchestrator code.
  - [`bot/engine/worker.py`](bot/engine/worker.py:177) — poller and job execution (`UnifiedWorkerManager`).
  - [`bot/engine/tool_runner.py`](bot/engine/tool_runner.py:22) — centralized tool execution and error logging.

- [`tools/`](tools/): registered tools. Example evidence:
  - [`tools/scraper/tool.py`](tools/scraper/tool.py:28) — end-to-end scraper tool implementation.
  - [`tools/scraper/extraction.py`](tools/scraper/extraction.py:1) — per-article processing and link extraction.

- [`database/`](database/): database connection, writer, logs writer, schemas, and management utilities.
  - [`database/connection.py`](database/connection.py:12) — DB paths and connection helpers (`DatabaseManager`, `LogsDatabaseManager`).
  - [`database/writer.py`](database/writer.py:66) — main DB single-writer thread and APIs `enqueue_write`, `start_writer`.
  - [`database/logs_writer.py`](database/logs_writer.py:58) — dedicated logs writer thread with fallback behavior.
  - [`database/schemas/logs.py`](database/schemas/logs.py:3) — DDL for `logs` table used by the logs DB.
  - [`database/management/lifecycle.py`](database/management/lifecycle.py:36) — lifecycle and reconciliation logic.

- [`utils/`](utils/): library-level helpers.
  - [`utils/logger/`](utils/logger/): dual-logger API and contract. Primary: [`utils/logger/core.py`](utils/logger/core.py:27) and [`utils/logger/__init__.py`](utils/logger/__init__.py:4).
  - [`utils/browser_daemon.py`](utils/browser_daemon.py:32) — Chrome driver lifecycle manager.
  - [`utils/id_generator.py`](utils/id_generator.py:14) — ULID generator used across the codebase.
  - [`utils/startup/`](utils/startup/) — startup orchestration; evidence: [`utils/startup/database.py`](utils/startup/database.py:13) and [`utils/startup/core.py`](utils/startup/core.py:20).

- [`tests/`](tests/): tests observed. Example: [`tests/test_backup.py`](tests/test_backup.py:1).

- [`deprecated/`](deprecated/): legacy code. Its presence is an explicit repository artifact used below in the "Changes" section.


4.  CORE CONCEPTS & DOMAIN MODEL

- Jobs & lifecycle (concrete): The code treats jobs as records in a `jobs` table (see `INSERT` usage at [`api/routes.py:138`](api/routes.py:138) and update operations in [`bot/engine/worker.py`](bot/engine/worker.py:253)). Job states referenced in code include `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `INTERRUPTED`, `PENDING_CALLBACK`, `CANCELLING`.

- Dual logging contract (explicit code contract): The runtime exposes a dual-logger where:
  - Console output is produced through Python's `logging` logger wrapped by `SumAnalLogger`: see [`utils/logger/core.py:34`](utils/logger/core.py:34).
  - Structured persistence of full details is written to the logs DB via [`database/logs_writer.py`](database/logs_writer.py:58).
  - The developer contract text is present in [`utils/logger/__init__.py`](utils/logger/__init__.py:4) with the explicit rule: "payload=None is a CONTRACT VIOLATION" (see [`utils/logger/__init__.py:12`](utils/logger/__init__.py:12)). The runtime enforces a warning when `payload` is omitted in [`utils/logger/core.py`](utils/logger/core.py:54).

- Logs schema (concrete): [`database/schemas/logs.py`](database/schemas/logs.py:3) defines the `logs` table with these columns (as present now): `id, job_id, tag, level, status_state, message, payload_json, event_id, error_json, timestamp` (create DDL at [`database/schemas/logs.py:3`](database/schemas/logs.py:3)).

- Single-writer invariant: All writes to main DB and logs DB are serialized through dedicated writer threads (`enqueue_write` in [`database/writer.py`](database/writer.py:141) and `logs_enqueue_write` in [`database/logs_writer.py`](database/logs_writer.py:101)). Reader connections refresh based on a write-generation counter implemented in [`database/writer.py`](database/writer.py:58) and [`database/logs_writer.py`](database/logs_writer.py:33) and observed by `DatabaseManager` and `LogsDatabaseManager` in [`database/connection.py`](database/connection.py:37, 127).

- ULID id generation: Unique IDs used across jobs and logs are generated by [`utils/id_generator.py`](utils/id_generator.py:14).


5.  DETAILED BEHAVIOR (normal execution, edge cases, and error handling)

- Startup sequence (explicit in code): [`app.py`](app.py:43) uses an async lifespan that calls into the startup orchestrator and then starts/flushes writers and the browser daemon at shutdown. Startup database initialization and the logs DB "fresh start" behavior is implemented in [`utils/startup/database.py`](utils/startup/database.py:13). That module explicitly unlinks `LOGS_DB_PATH` on startup if it exists (fresh start behavior) at [`utils/startup/database.py:15`](utils/startup/database.py:15).

- Job enqueue and execution (observed):
  - Job enqueue: API writes a `jobs` record with `status=QUEUED` via `enqueue_write(...)` in [`api/routes.py:138`](api/routes.py:138).
  - Polling: `UnifiedWorkerManager._run_loop` polls for rows in `jobs` and spawns threads to run jobs using `spawn_thread_with_context(...)` (see [`bot/engine/worker.py:206`](bot/engine/worker.py:206)).
  - Tool execution: `run_tool_safely(...)` centralizes tool error handling and logs errors to the logs DB via `logs_enqueue_write(...)` (`[`bot/engine/tool_runner.py:32`](bot/engine/tool_runner.py:32)).

- Failure modes and handling (explicit examples in code):
  - Corrupted DB: `run_database_lifecycle()` and `SchemaReconciler` can recreate missing tables; destructive reset behavior is gated by `SUMANAL_ALLOW_SCHEMA_RESET` as referenced in [`utils/startup/database.py`](utils/startup/database.py:81).
  - Log write queue overflow: `logs_enqueue_write()` uses a bounded queue (`maxsize=5000`) and a 5-second blocking grace before falling back to a persistent `logs/fallback.log` (see [`database/logs_writer.py:14`](database/logs_writer.py:14) and fallback writer [`database/logs_writer.py:39`](database/logs_writer.py:39)).
  - Writer not running: `enqueue_write` attempts to `start_writer()` and logs (but will drop writes if it cannot start): see [`database/writer.py:150`](database/writer.py:150) and the subsequent `write_queue.put_nowait` handling at [`database/writer.py:167`](database/writer.py:167).

- Configuration and runtime toggles: Config is read from [`config.py`](config.py:1) (module presence observed) and environment variables are honored — e.g., `SUMANAL_ALLOW_SCHEMA_RESET` referenced in [`utils/startup/database.py`](utils/startup/database.py:81).


6.  PUBLIC INTERFACES (precise endpoints & entry points)

- HTTP REST API (evidence: [`api/routes.py`](api/routes.py:1)):
  - `GET /` — health/version (implemented in [`app.py`](app.py:136)).
  - `POST /api/tools/{tool_name}` — enqueue a job (see [`api/routes.py:51`](api/routes.py:51)).
  - `GET /api/jobs/{job_id}` — get job status and latest payload (see [`api/routes.py:223`](api/routes.py:223)).
  - `DELETE /api/jobs/{job_id}` — request cancellation (see [`api/routes.py:279`](api/routes.py:279)).
  - Backup endpoints: `POST /api/backup/export` and `POST /api/backup/restore` implemented in [`api/routes.py:159`](api/routes.py:159) and [`api/routes.py:196`](api/routes.py:196).

- Tool interface (evidence: [`tools/base.py`](tools/base.py:1) and usages in `tools/*`): Tools follow a synchronous/async execution interface; the `ScraperTool` implements `run`/`_run_internal` in [`tools/scraper/tool.py`](tools/scraper/tool.py:33).

- Programmatic APIs:
  - `log.dual_log(...)` — public logging API from `utils.logger.get_dual_logger()` (`[`utils/logger/core.py:193`](utils/logger/core.py:193)).
  - Database enqueue functions: `enqueue_write(...)` (`[`database/writer.py:141`](database/writer.py:141)) and `logs_enqueue_write(...)` (`[`database/logs_writer.py:101`](database/logs_writer.py:101)).


7.  STATE, PERSISTENCE, AND DATA

- Storage locations (concrete):
  - Main DB: path configured in [`database/connection.py:25`](database/connection.py:25) (`DB_PATH = Path("data") / "sumanal.db"`).
  - Logs DB: path configured in [`database/connection.py:26`](database/connection.py:26) (`LOGS_DB_PATH = Path("data") / "logs.db"`).
  - Artifacts directory: created under [`tools/scraper/tool.py`](tools/scraper/tool.py:317) via `write_artifact(...)` which writes to `artifacts/<tool_name>/`.

- Data formats:
  - SQLite for structured data (main DB and logs DB). `logs.payload_json` stores JSON strings (`[`database/schemas/logs.py`](database/schemas/logs.py:3)).
  - Parquet used for backups/export (export logic present in [`database/backup/exporter.py`](database/backup/exporter.py:1); tests import `pyarrow`/`pandas` in [`tests/test_backup.py`](tests/test_backup.py:6)).
  - Vector/BLOB formats for embeddings when `sqlite_vec` is not available: [`database/connection.py`](database/connection.py:16) treats `sqlite_vec` as optional and degrades to BLOB storage.

- Reset/migration behavior (explicit):
  - On startup the lifecycle reconciler inspects schemas and may create or repair tables; destructive reset is controlled by `SUMANAL_ALLOW_SCHEMA_RESET` (`[`utils/startup/database.py`](utils/startup/database.py:81)).
  - The logs DB is subject to a "fresh start" policy on initialization: the code explicitly unlinks `logs.db` (and WAL/SHM sidecars) before starting the logs writer (`[`utils/startup/database.py:15`](utils/startup/database.py:15)).


8.  DEPENDENCIES & INTEGRATIONS (evidence-only)

- Libraries observed imported in code: `fastapi` and `uvicorn` (`[`app.py:16`](app.py:16)), `pydantic` (`[`api/routes.py:11`](api/routes.py:11)), `pandas` & `pyarrow` in tests (`[`tests/test_backup.py:6`](tests/test_backup.py:6)), `selenium`/`botasaurus` usage in browser code (`[`utils/browser_daemon.py:16`](utils/browser_daemon.py:16)), `httpx` used in callback code (`[`bot/engine/worker.py:23`](bot/engine/worker.py:23)). These are concrete requirements present by import statements.

- Integration points and external assumptions (code-level):
  - External LLM endpoints / clients are referenced by the client factory code in `clients/` and by callers that expect `get_llm_client(...)` (`[`bot/engine/tool_runner.py:15`](bot/engine/tool_runner.py:15)).
  - An external callback service (AnythingLLM) is exercised by `_do_callback_with_logging` in [`bot/engine/worker.py`](bot/engine/worker.py:37).
  - Optional `sqlite_vec` extension: [`database/connection.py`](database/connection.py:16) attempts to load `sqlite_vec` if available and the code contains fallbacks if it is absent.


9.  SETUP, BUILD, AND EXECUTION (explicit commands and preconditions)

- Basic steps (reproducible from code evidence):
  1. Create a Python 3 environment and install packages referenced by imports (example modules observed: `fastapi`, `pydantic`, `httpx`, `pyarrow`, `pandas`, `psutil`, `botasaurus`/`selenium`). The repository contains `requirements.txt` at the root.
  2. Ensure writable `data/` and `artifacts/` directories; [`database/connection.py`](database/connection.py:57) creates parent directories where necessary.
  3. Launch the application as the inline comment at the top of [`app.py`](app.py:8) suggests:

     ```bash
     python -m uvicorn app:app --reload --port 8000
     ```

- Platform assumptions: Python 3.10+ (typing syntax used) and presence of Chrome/Chromium if browser-bound tools are executed — [`utils/browser_daemon.py`](utils/browser_daemon.py:75) expects `CHROME_USER_DATA_DIR` in config.


10. TESTING & VALIDATION (what exists and gaps)

- Tests present: [`tests/test_backup.py`](tests/test_backup.py:1) (evidence). The test file contains explicit assertions around `pyarrow` schemas and embedding validation (see tests importing `pyarrow` and `pandas` in [`tests/test_backup.py:6`](tests/test_backup.py:6)).

- Test gaps visible from the repository:
  - Core control paths lack test coverage visible in the repo: [`bot/engine/worker.py`](bot/engine/worker.py:177) and [`api/routes.py`](api/routes.py:1) (no corresponding test files referencing those modules are present in `tests/`).
  - The logging pipeline (`utils/logger/`) has no unit tests visible in `tests/`.
  - The browser lifecycle (`utils/browser_daemon.py`) does not have automated tests in `tests/`.


11. KNOWN LIMITATIONS & NON-GOALS (evidence-based)

- Hard-coded or explicit constraints visible in code:
  - `session_id` hardcoded to the string "0" as a fallback in multiple API code paths ([`api/routes.py`](api/routes.py:53)).
  - Single API key approach enforced by `verify_api_key` in [`app.py`](app.py:26) — only a simple API key header is checked.
  - Single-writer SQLite architecture is used everywhere (see writer threads in [`database/writer.py`](database/writer.py:228) & [`database/logs_writer.py`](database/logs_writer.py:117)), limiting horizontal scale.

- Specific implementation limitations observed in current files (concrete items needing attention):
  - Legacy log inserts in [`tools/scraper/tool.py`](tools/scraper/tool.py:75) use `enqueue_write(...)` to write directly to the `logs` table (examples at lines 75 and 158). These bypass the dedicated logs writer ([`database/logs_writer.py`](database/logs_writer.py:101)) and do not conform to the current `logs` schema defined in [`database/schemas/logs.py`](database/schemas/logs.py:3). Recommended action: migrate these inserts to `logs_enqueue_write(...)` and update INSERT statements to match the current column set (`id, job_id, tag, level, status_state, message, payload_json, event_id, error_json, timestamp`).
  - [`utils/logger/core.py`](utils/logger/core.py:102) falls back to serializing the payload as a JSON string when the primary structured serialization fails (see the payload serialization at [`utils/logger/core.py:102`](utils/logger/core.py:102)). This can store a JSON string in `payload_json` instead of a structured JSON object, violating the logger contract documented in [`utils/logger/__init__.py`](utils/logger/__init__.py:12). Recommended action: ensure `_serialize_payload` returns JSON-serializable objects and avoid string fallback; prefer explicit error handling and a safe fallback object.
  - Several call sites still pass `payload=None` (e.g., [`utils/browser_daemon.py`](utils/browser_daemon.py:217)), which violates the logger contract requiring a non-empty dict. Recommended action: update call sites to provide an explicit payload object (`{}` or structured context) and audit the codebase for other occurrences. A helper script exists to help find violations: [`scripts/check_log_contract.py`](scripts/check_log_contract.py:1).


12. CHANGE SENSITIVITY (what to watch when changing the code)

- Most fragile areas (evidence and rationale):
  1. Database lifecycle and schema reconciliation (`database/management/lifecycle.py`) — code executes DDL such as `DROP TABLE` and `CREATE TABLE` based on schema reconciliation. Mistakes here can destroy data ([`database/management/lifecycle.py`](database/management/lifecycle.py:36)). Confidence: HIGH (explicit DDL operations present).
  2. Writer threading model (`database/writer.py` and `database/logs_writer.py`) — single-writer invariants and generation counters are used by `DatabaseManager`/`LogsDatabaseManager` to refresh read connections (`[`database/writer.py`](database/writer.py:66); [`database/connection.py`](database/connection.py:41,127)). Changes to this model require coordinated updates across readers and writers. Confidence: HIGH.
  3. Browser lifecycle/SoM instrumentation (`utils/browser_daemon.py` and `utils/som_utils`) — depends on fragile external browser process internals and JS injection. Confidence: HIGH.

- Modules easiest to extend (evidence):
  - Tool addition: adding a new tool module under `tools/` is straightforward; [`tools/registry.py`](tools/registry.py:1) performs discovery and [`api/routes.py`](api/routes.py:55) calls `REGISTRY.load_all()` at enqueue time. Confidence: HIGH.
