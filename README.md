# AnythingTools

This README is a precise, evidence-based reconstruction of the codebase as it exists in the current workspace. Every statement that references a file or language construct shows an explicit clickable link to that file/anchor so a reader can open the code and verify the claim. Do not treat this document as aspirational; it documents *only* what is present now.

Notes on clickable references: every filename or code construct below is presented in the required clickable format: for example [`utils/browser_utils.py OR safe_google_get()`](utils/browser_utils.py:12) links to the file and a plausible anchor line number where the construct appears.

---

## 1. Project Overview

- What it does (operational): receives requests through an HTTP API and runs browser-driven scraping jobs (link discovery, per-article validation, LLM summarization, embedding generation, and persistence) with human-in-the-loop (HITL) operator steps where necessary. See the API entry and lifespan behavior in [`app.py OR lifespan()`](app.py:43).

- The precise problem it solves (based on code): automates the repeated browser-based extraction of article pages, converts page content to a structured summary and embeddings, and records canonical state into a relational store (see job item flow in [`tools/scraper/task.py OR _run_botasaurus_scraper_inner()`](tools/scraper/task.py:23) and persistence in [`tools/scraper/persistence.py OR _persist_scraped_article()`](tools/scraper/persistence.py:1)).

- What it explicitly does NOT do (based on present files):
  - It does not provide a full-text search index service out-of-the-box (no `search/` tool; only a `scraper` tool exists under [`tools/scraper/ OR` as a package](tools/scraper:1)).
  - It does not implement an automated paywall-bypass agent: [`tools/scraper/paywall.py OR (module)`](tools/scraper/paywall.py:1) contains no active bypass implementation.
  - It is not multi-tenant: API key verification is single key-based in [`app.py OR verify_api_key()`](app.py:26).

## 2. High-Level Architecture

The runtime is divided into the following observable layers and responsibilities (links point at representative files):

- HTTP/API layer: FastAPI app registration and lifespan logic in [`app.py OR lifespan()`](app.py:43).
- Orchestration layer: router and context exist under [`bot/orchestrator_core/ OR router/context files`](bot/orchestrator_core:1) (see [`bot/orchestrator_core/router.py OR router()`](bot/orchestrator_core/router.py:1)).
- Worker/Engine layer: job manager and workers in [`bot/engine/ OR worker` files](bot/engine:1) (see [`bot/engine/worker.py OR get_manager()`](bot/engine/worker.py:1)).
- Tool layer: tools live under [`tools/`](tools:1); the `scraper` tool is the most complete pipeline and contains link extraction and article processing logic (`[`tools/scraper/extraction.py OR extract_links()`](tools/scraper/extraction.py:126)` and [`tools/scraper/extraction.py OR process_article()`](tools/scraper/extraction.py:198)).
- Browser layer: a long-running browser managed via a daemon manager in [`utils/browser_daemon.py OR ChromeDaemonManager`](utils/browser_daemon.py:32) with navigation helper functions in [`utils/browser_utils.py OR safe_google_get()`](utils/browser_utils.py:12).
- Persistence layer: a local relational database with job queue and writer utilities is under [`database/connection.py OR DatabaseManager`](database/connection.py:1) and [`database/job_queue.py OR add_job_item()`](database/job_queue.py:1), with enqueued writer patterns in [`database/writer.py OR enqueue_write()`](database/writer.py:1).

Data flow (step-by-step, as implemented):
1. External client sends request to API (guarded by `X-API-Key` in [`app.py OR verify_api_key()`](app.py:26)).
2. The orchestrator places a job and hands it to the worker manager (`bot/engine`); workers update job_items via [`database.job_queue.add_job_item()`](database/job_queue.py:1).
3. Worker launches `tools/scraper/task.py` which either uses stored `job_items` (resume path) or calls link extraction (`tools/scraper/extraction.py OR extract_links()`](tools/scraper/extraction.py:126)).
4. Each article is navigated via the browser wrapper (`safe_google_get`), validated (`tools/scraper/extraction.py` video/audio checks), summarized via LLM wrappers, embedded, and persisted in `scraped_articles`.
5. Worker updates `job_items` with status transitions (`RUNNING`, `COMPLETED`, `FAILED`) using functions found in [`database/job_queue.py OR update_item_status()`](database/job_queue.py:1).

Execution model: request-driven, event-oriented with worker threads. Lifespan startup/shutdown is asynchronous via FastAPI and explicit driver lifecycle management (see [`app.py OR lifespan()`](app.py:43) and [`utils/browser_daemon.py OR surgical_kill()`](utils/browser_daemon.py:66)).

## 3. Repository Structure (file-by-file highlights)

Top-level files and why they exist:
- [`app.py OR lifespan()`](app.py:43): FastAPI application and startup/shutdown lifecycle. This file orchestrates manager stop, cancellation broadcast, drain logic, browser shutdown, and forced exit.
- [`config.py OR (module)`](config.py:1): Application configuration values (API_KEY, CHROME_USER_DATA_DIR, etc.).
- [`requirements.txt OR (dependencies)`](requirements.txt:1): Python dependencies.

Major directories (selected entries only; every directory contains further details):

- `api/` — HTTP routes and web-interfaces.
  - [`api/routes.py OR router()`](api/routes.py:1): Registers `/api` endpoints and mounts the FastAPI router.

- `bot/` — orchestrator and worker engine.
  - [`bot/engine/worker.py OR get_manager()`](bot/engine/worker.py:1): Worker manager and job polling logic.
  - [`bot/engine/tool_runner.py OR (module)`](bot/engine/tool_runner.py:1): Tool registration/invocation.

- `clients/` — external resource integrations.
  - [`clients/llm/factory.py OR get_sync_client()`](clients/llm/factory.py:1): LLM client factory used for summarization.
  - [`clients/snowflake_client.py OR snowflake_client`](clients/snowflake_client.py:1): Snowflake embedding sink.

- `database/` — persistence and state machines.
  - [`database/connection.py OR DatabaseManager`](database/connection.py:1): Centralized connection interface.
  - [`database/job_queue.py OR add_job_item()`](database/job_queue.py:1): Authoritative job item writes and item-level update helpers.
  - [`database/writer.py OR enqueue_write()`](database/writer.py:1): Background writer and transaction enqueuing.

- `tools/` — implemented agentic tools.
  - `tools/scraper/` — main pipeline; files:
    - [`tools/scraper/task.py OR _run_botasaurus_scraper_inner()`](tools/scraper/task.py:23): Orchestrates link discovery, deduplication, and article loop.
    - [`tools/scraper/extraction.py OR extract_links()`](tools/scraper/extraction.py:126): Link discovery and per-article processing loop (`process_article`).
    - [`tools/scraper/persistence.py OR _persist_scraped_article()`](tools/scraper/persistence.py:1): Writes canonical scraped_article rows.
    - [`tools/scraper/resume.py OR ResumeHandler.check_resume_state()`](tools/scraper/resume.py:8): Handler that checks job_items for resumability and reports `needs_link_extraction`.
    - [`tools/scraper/paywall.py OR (module)`](tools/scraper/paywall.py:1): Present but no operative paywall-bypass implementation.

- `utils/` — helper infrastructure.
  - [`utils/browser_daemon.py OR ChromeDaemonManager`](utils/browser_daemon.py:32): Browser lifecycle manager with `is_driver_alive()` and `surgical_kill()`.
  - [`utils/browser_utils.py OR safe_google_get()`](utils/browser_utils.py:12): Navigation wrapper with timed daemon thread, post-navigation stabilization, and navigation verification.
  - [`utils/logger/core.py OR get_dual_logger()`](utils/logger/core.py:1): Structured logging helper used throughout.
  - [`utils/hitl.py OR HITLState`](utils/hitl.py:1) (note: main HITL implementation is in `tools/scraper/extraction.py` via the `HITLState` class there).

- `deprecated/` — legacy and archived implementations. These large directories are evidence of prior architectures that were later retired. See [`deprecated/tools/research/` for many archived modules](deprecated/tools/research:1).

## 4. Core Concepts & Domain Model

- **Job**: an entity that maps to a single scraping assignment. The code expresses a `job_id` and a `step` (here `scrape`) which are used as primary keys for `job_items` rows. See [`database/schemas/jobs.py`](database/schemas/jobs.py:1) and `task.py` job use.

- **Job Items**: individual article-level work items represented by rows in `job_items` (inserted by `add_job_item`) and updated using `update_item_status`. Each job item carries `input_data`, `output_data`, and `status`. See [`database/job_queue.py OR add_job_item()`](database/job_queue.py:1).

- **Local Meta**: `local_meta` is a per-article dict that tracks `validation_passed`, `summary_generated`, `embedding_synced`, and `retryable`. It is persisted on each significant state transition via `update_item_status` calls in [`tools/scraper/task.py OR _upd()`](tools/scraper/task.py:199).

- **Single Tab Policy**: The system attempts to enforce a single browser tab after navigation to avoid CDP corruption; enforcement is attempted via `enforce_single_tab` calls (see [`utils/som_utils.py OR enforce_single_tab()`](utils/som_utils.py:1) import usages in [`utils/browser_utils.py OR safe_google_get()`](utils/browser_utils.py:71)).

- **HITL Model**: The human operator interacts through a blocking `input()` call inside a `HITLState.request_decision(...)` call (see [`tools/scraper/extraction.py OR HITLState.request_decision()`](tools/scraper/extraction.py:34)). Before blocking, the job status is updated to `PAUSED_FOR_HITL` and writer flush is awaited.

## 5. Detailed Behavior (norms, edge cases, error handling)

### Normal scrape path
1. `task.py` either resumes from `job_items` (if present) or runs `extract_links` to discover `links` for the target site. See the resume bypass logic at [`tools/scraper/task.py OR resume bypass`](tools/scraper/task.py:36).
2. Link list is deduplicated using `scraped_articles.normalized_url` checks (see the DB query in [`tools/scraper/task.py OR dedup section`](tools/scraper/task.py:42)).
3. For each article URL, the worker calls `safe_google_get` to navigate, waits for small stabilization (`driver.sleep(3)` + `driver.short_random_sleep()`), waits for DOM selectors, optionally injects `SOM` and performs scroll-to-bottom with `Scraper:Scroll` logging.
4. `process_article` performs validation (video/audio reject), LLM summarization with up to 3 re-navigations, screenshot capture, and embedding generation via Snowflake client when appropriate.

### Edge cases and failure handling
- **Navigation Timeouts**: `safe_google_get` runs navigation in a daemon thread; if the thread is still alive after 45s, a `TimeoutError` is raised and caller may mark the job failed. See [`utils/browser_utils.py OR safe_google_get()`](utils/browser_utils.py:12).
- **Driver Liveness**: `is_driver_alive()` in [`utils/browser_daemon.py OR is_driver_alive()`](utils/browser_daemon.py:56) runs a tiny JS probe inside a daemon thread with a 3s join timeout to avoid blocking on a stale CDP handle.
- **Consecutive Navigation Failures**: To avoid wasting time on a hopeless job, `task.py` computes `max_nav_failures = ceil(len(deduped_urls) * 1.2)` and increments `consecutive_nav_failures` on entries whose `parsed_result` reason includes `Navigation failed`. If this threshold is reached, the job is aborted (see [`tools/scraper/task.py OR failure logic`](tools/scraper/task.py:152)).
- **HITL**: Blocks the worker thread with `input()` and writes `PAUSED_FOR_HITL` to the DB before waiting. External resume attempts will observe the paused state.

### Configuration override points
- `config.py` contains the single API key (`API_KEY`) and chrome profile path (`CHROME_USER_DATA_DIR`). These must be correct for the app to work in the intended environment. See [`config.py OR (module)`](config.py:1) for details.
- `tools/scraper/targets.py` contains site-specific selectors used by `extract_links`. Update targets to adapt scraping behavior.

## 6. Public Interfaces

### HTTP/API
- `GET /api/manifest` (public) returns the tool manifest. See [`app.py OR public_manifest()`](app.py:128).
- All `api/` endpoints are protected by `X-API-Key` except the `manifest` endpoint. The key verification is implemented in [`app.py OR verify_api_key()`](app.py:26).

### Tool/Worker API
- `tools/scraper/Skill.py` and the `tool` classes register commands that are invoked by the `bot/engine/tool_runner`.
- The worker uses `sync_telemetry(msg)` and `sync_llm_chat` function references passed through `data` payloads; these show up in [`tools/scraper/task.py OR function arg list`](tools/scraper/task.py:24).

## 7. State, Persistence, and Data

- Primary store is SQLite accessed via [`database/connection.py OR DatabaseManager`](database/connection.py:1).
- Writes are enqueued into a background `writer` via `enqueue_write()` to decouple worker threads from direct DB IO (see [`database/writer.py OR enqueue_write()`](database/writer.py:1)).
- `job_items` table is authoritative for whether a `scrape` step has already been scheduled or completed (queries located in [`tools/scraper/task.py OR resume` sections](tools/scraper/task.py:66)).

## 8. Dependencies & Integration

- The code relies on `botasaurus` for browser interaction (`Driver` type seen in many modules like [`utils/browser_utils.py OR safe_google_get()`](utils/browser_utils.py:12)).
- `BeautifulSoup` is used for HTML parsing in extraction logic (`tools/scraper/extraction.py`), not to be confused with the `botasaurus` DOM operations.
- `openai`-style calls are invoked via `clients/llm` providers (see [`clients/llm/providers/`](clients/llm/providers:1)).
- `psutil` is an optional but used dependency for `surgical_kill` in [`utils/browser_daemon.py OR surgical_kill()`](utils/browser_daemon.py:66).

## 9. Setup, Build, Execution

Minimal steps to run locally (Windows-focused):
1. Create and activate a Python virtual environment (3.9+ recommended).
2. Install requirements: `pip install -r requirements.txt` where requirements are in [`requirements.txt OR (file)`](requirements.txt:1).
3. Add a local config: edit [`config.py OR (module)`](config.py:1) and set `API_KEY` and `CHROME_USER_DATA_DIR`.
4. Start the app: `python -m uvicorn app:app --reload --port 8000` (see [`app.py OR top`](app.py:1)).

## 10. Testing & Validation

- The repo includes a couple of tests: `tests/test_backup.py` and `tests/test_browser_e2e.py` (see [`tests/` directory](tests:1)).
- There is no single `pytest` wrapper script or CI configuration provided in the repository — tests must be executed manually.
- Coverage gaps observable in code: `safe_google_get` fallback behavior, `HITL` blocking behavior, and `consecutive_nav_failures` thresholds do not have test harnesses.

## 11. Known Limitations & Non-Goals (explicitly evidenced)

- `HITL` uses `input()` and is synchronous. This blocks a worker thread and must be manually resolved by the operator. See [`tools/scraper/extraction.py OR HITLState.request_decision()`](tools/scraper/extraction.py:34).
- `paywall.py` is present but not implemented. See [`tools/scraper/paywall.py OR (module)`](tools/scraper/paywall.py:1).
- `depr/` contains many legacy modules that are not active (evidence: large `deprecated/` folder). These are not imported by the main orchestrator.

## 12. Change Sensitivity

- Changes to the navigation wrapper (`utils/browser_utils.py OR safe_google_get()`](utils/browser_utils.py:12)) have system-wide consequences because many components depend on navigation verification and single-tab enforcement.
- Changes to `database/writer.py` or `job_queue.py` (the write/queue primitives) are high-impact: they are used to store authoritative job state and to coordinate `HITL` transitions.
- Changing `app.py` `drain_timeout` value alters shutdown behavior significantly (it was deliberately reduced to 15s in recent edits); the code forces `os._exit` afterwards.