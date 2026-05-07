# AnythingTools

## 1. Project Overview

**AnythingTools** is a FastAPI-based asynchronous automation platform designed for "Synchronized Browser Scouting". Its operational definition is the execution of a `Scraper Tool` (in `tools/scraper`) which navigates a headful Chrome browser, extracts article data from explicitly whitelisted target sites, performs LLM curation of the top 10 findings, and synchronizes the results into the local ecosystem (Artifacts directory, JSON backup, and optional AnythingLLM integration).

**Problem Solved:**
It solves the problem of extracting structured, validated "Top 10" article insights from specific web sources in a reliable, agentive manner. It handles the friction of browser lifecycle management (zombie processes, profile locking) and provides explosion-proof data persistence.

**Capabilities (Explicit):**
*   **Target-Specific Extraction:** Supports a fixed map of target domains (`VALID_TARGET_NAMES`).
*   **Headful Browser Management:** Manages a persistent Chrome user data directory (`CHROME_USER_DATA_DIR`) and zombie cleanup (`cleanup_zombie_chrome`).
*   **LLM Curation:** Uses an LLM (via `UnifiedLLM`) to curate slim metadata into a top 10 list.
*   **Hybrid Artifact Persistence:** Writes raw JSON output (`artifacts/scraper/`) and synced metadata (`top10.json`).
*   **AnythigLLM Synchronization:** Drops artifacts into `custom-documents` (via `artifact_manager`) and manages chat callbacks.
*   **Telegram State Routing:** (Optional) Pushes status updates to Telegram.

**Explicit Non-Goals (Code Evidence):**
*   **No PDF/Vector Search:** The `deprecated/` folder contains `pdf_search` tool code, but it is omitted from `tools/registry.py` loading.
*   **No Generic Browsing:** `skill` and `macros` are deprecated. The codebase strictly enforces a "Tool" execution model.
*   **Nothing Aspirational:** The documentation does not define a feature unless `config.py` or a subclass exposes it as an environment variable or method.

## 2. High-Level Architecture

### Component Overview
1.  **API Layer (`app.py`):**:
    *   FastAPI entrypoint with `lifespan` (startup/shutdown).
    *   Security: `verify_api_key` (Header `X-API-Key`).
    *   Routes: `/api` (Authenticated), `/api/manifest` (Public).
    *   **Startup Logic:** Defined in `utils/startup/core.py` (see *Detailed Behavior*).

2.  **Job Engine (`bot/engine/worker.py`)**:
    *   **Polling Loop:** `UnifiedWorkerManager` polls `database/job_queue` (assumed SQL via `sqlite3`/`job_queue.py`).
    *   **Centralized Execution:** Instantiates tools via `tools/registry` and executes `self.run()`.
    *   **Crash Recovery:** Re-activates interrupted jobs (checked via `is_resume`).

    *   **Callback Output:** Sends structured Markdown payloads to `ANYTHINGLLM_BASE_URL` (configured via `config.py`).

3.  **Minimum Base Tools (Registry Loading)**:
    *   **Scraper:** *Core logic defined later.*
    *   **Draft Editor:** `tools/draft_editor` (Stateful text editor).
    *   **Publisher:** Pushes content to external systems.
    *   **Batch Reader:** Hybrid Vector/Keyword search (Weighted: `BATCH_READER_VECTOR_WEIGHT`).

4.  **Data Persistence (State)**:
    *   **Parquet Backups:** *Backup Runner (Automated?)*.
    *   **Job Queue:** `database/job_queue.py` (Write-heavy via `enqueue_write`).
    *   **Logs:** `utils/logger` (Structured, batched writes).
    *   **Artifacts:** `artifacts/scraper/` (Write heavy, atomic replacement).

5.  **SoM Observation**:
    *   `utils/som_utils.py` (Injects `data-ai-id`).
    *   `utils/observation_adapter.py` (Visual analysis, but effectively "Standard HTML" extraction).
    *   *Note: The client adds `Wait for Quiet` (45s) to handle extension DOM mutation.* (See *Changes*).

### Data Flow (Scrape Execution)
1.  **API Request:** `POST /api/execute` (Likely) -> `Worker.run` -> `add_job`.
2.  **Poller:** `UnifiedWorkerManager` sees `RUNNING` job -> `scraper_tool.run`.
3.  **Scraper Tool:**
    *   **Browser Lock:** `utils/browser_lock.py`.
    *   **Target:**
        *   `safe_google_get(url)` (Replaced `requests` with `httpx`).
        *   ***Wait for Quiet:*** `driver.sleep(45s)` (Self-imposed "Sleep" tool to handle extensions).
        *   **SoM:** `inject_som(driver)`.
        *   **Native Waits:** `_safe_wait_for_any_selector` (Threaded `driver.wait_for_element`).
        *   **Greedy Extraction:** `extract_hybrid_html` (Only >40 chars, wrappers stripped).
    *   **Validation:** `sync_llm_chat` (Variable context limit via `LLM_CONTEXT_CHAR_LIMIT`).
    *   **Curation:** `sync_llm_chat` (JSON Schema).
    *   **Artifacts:** `write_artifact` (top_10.json).
    *   **Backup:** `BackupRunner.run(mode="delta")`.
    *   **Callback:** `_do_callback_with_logging` -> `AnythigLLM` (Msgpack format, "Custom Documents" logic).

4.  **Shutdown:** `app.py` lifespan handles drain (60s timeout), `daemon_manager.shutdown` + `sync_writes`.

## 3. Repository Structure

*   `app.py`: Top-level lifespan, middleware, router inclusion.
*   `config.py`: Merged environment variables. Controls all timeouts, limits, and feature flags (e.g., `BACKUP_ENABLED`).
*   `requirements.txt`: Standard Python dependencies.
*   **`bot/`**: Logic for Job Polling and State Management.
    *   `engine/worker.py`: `UnifiedWorkerManager` (Poller) and `_do_callback_with_logging`.
    *   `orchestrator/`: Context and Eviction logic (internal).
*   **`clients/`**: LLM wrappers.
    *   `llm/factory.py`: Singleton `UnifiedLLM`.
    *   `providers/azure.py`: Uses `AZURE_KEY`.
*   **`database/`**:
    *   `job_queue.py`: `add_job_item`, `update_item_status` (SQLite writes).
    *   `writer.py`: `enqueue_write` (Batch buffer).
    *   `backup/`:
        *   `runner.py`: `BackupRunner.run(mode, trigger_type)`.
        *   `config.py`: `BACKUP_BATCH_SIZE`, `BACKUP_COMPRESSION`.
*   **`deprecated/`**:
    *   `tools/research/`, `pdf_search/`, `macros/`. *These exist but are not whitelisted in `registry.py`.*
*   **`utils/`**:
    *   `startup/`:
        *   `core.py`: `StartupOrchestrator` (Tiered concurrent execution).
        *   `browser.py`: `load_zombie_chrome`, `warmup_browser`.
        *   `server.py`: `get_mount_artifacts_step` (FastAPI `Mount`).
    *   `browser_daemon.py`: `daemon_manager` (Kills persistent chrome context).
    *   `observation_adapter.py`: Visual / Data analysis.
    *   `sor_utils.py`: `inject_som`, `frame_mark_elements.js`.
    *   `source_context.py`: *Unclear usage without reading full file.*
    *   `telegram/`: Batching wrapper (`3.1s` delay, `4000` char limit).
*   **`tools/`**:
    *   `base.py`: `BaseTool`, `ToolResult` (Job Persistence).
    *   `registry.py`: **Explicit Whitelisting**: `["scraper", "draft_editor", "publisher", "batch_reader"]`.
    *   `scraper/`:
        *   `tool.py`: Main entry.
        *   `extraction.py`: **Wait for Quiet** logic (`driver.sleep`), `is_element_present` params.
        *   `task.py`: `_run_botasaurus_scraper` (The "Pipeline").
        *   `curation.py`: `Top10Curator` (LLM).
        *   `paywall.py`: `PaywallDetector`.
        *   `targets.py`: `TARGET_SITE_MAP`.

## 4. Core Concepts & Domain Model

*   **Do "Tool" (BaseTool):** All execution happens via a concrete `BaseTool` subclass. Legacy `m2`/`core` (deprecated) systems are replaced by `ToolResult` (Job vars).
*   **Job State:** Suffix `_meta` (JSON) used to resume status.
*   **CDP Health:**
    *   "Wait for Quiet": **45s** `driver.sleep` post-navigation to allow extensions to finish mutating DOM.
    *   **Custom Wrapper:** `_safe_wait_for_any_selector` wraps native polling with a **Thread Pool Executor**. It triggers a **CDP Ping Test** (timeout 5s) on hard failure.
    *   **Fail-Loud:** Fatal CDP errors raise `RuntimeError("Fatal CDP Stall")` to trigger `Surgical Kill`.
*   **Callback Format:** Structured usage of `f"TOOL_RESULT_CORRELATION_ID:{id}"` in `chat` mode. **No Base64 attachments** (Enforced in `worker.py` comment). Files go to `custom-documents/` via `artifact_manager`.
*   **SoM:** Uses `data-ai-id` (Aggregate `bid_`).

## 5. Detailed Behavior

### Normal Execution (Scrape)
1.  **Init:** `app.py` -> `run_startup`.
    *   Step 1 (Concurrent): Mount Artifacts, Cleanup Chrome, Init DB.
    *   Step 2 (Sequential): DB Migrations, `run_startup_recovery`.
    *   Step 3 (Concurrent): `load_tool_registry`, `warmup_browser`.
2.  **Execution:** `UnifiedWorkerManager` polls -> `scraper.run`.
3.  **Browser Logic (`_run_scraper` / `task.py`)**:
    *   `get_or_create_driver` (Singleton).
    *   `sync_telemetry` -> "Headful scraper".
    *   `safe_get(target_url)`. **Wait for 45s** (Extension Mutation Check).
    *   `_safe_wait_for_any_selector` (Native `wait_for_element`).
    *   `inject_som` (Start ID loop).
    *   `extract_hybrid_html` (Greedy extraction).
    *   `sync_llm_chat` (Validation - Nullable Image).
    *   `sync_llm_chat` (Curation - JSON Schema).
    *   `write_artifact` (Persist).
    *   `BackupRunner.run(mode="delta")`.
    *   `sync_llm_chat` (Callback).
4.  **Finish:** Output `json` payload.

### Error Handling / Fragility
*   **CDP Stall:** `ThreadPool` -> `Log` -> `raise RuntimeError`.
*   **Drain Timeout:** 60s in `app.py` shutdown.
*   **Context Length:** `LLM_CONTEXT_CHAR_LIMIT` (800k default).
*   **Client Error:** `4xx` retries (Halt). `5xx` retries.

## 6. Public Interfaces

### CLI / API
*   **`app.py`**: `uvicorn app:app --reload`.
    *   **Auth**: Header `X-API-Key`.
    *   **Public**: `/api/manifest` (Tool Schema).
    *   **Internal**: `/api/*` (Usage depends on `api/routes.py`).
*   **`config.py`**: **Sheer volume of variables makes explicit listing high-reconstructive-correctness.**
    *   `SUMANAL_ALLOW_SCHEMA_RESET`: Destructive mode.
    *   `BROWSER_SOM_HTML_CHAR_BUDGET`: 20k (Truncation).
    *   `LLM_CONTEXT_CHAR_LIMIT`: 800k.
    *   `BACKUP_ENABLED`: `true` (default).

## 7. State, Persistence, and Data

### Storage Types
1.  **Disk (Write-Heavy)**:
    *   `artifacts/scraper/`: Raw JSON, Top10 JSON.
    *   `chrome_profile/`: User data directory.
    *   `artifacts/` (Mapped): Derived droppings.
2.  **SQL (DBs)**:
    *   **Management**: `database/management/` (Lifecycle, Reconciler).
    *   **Schemas**: `database/schemas/` (Jobs, Logs, Finance, PDF, Tokens).
    *   **Job Queue**: `database/job_queue.py` (FIFO, `update_item_status`).
    *   **Logs**: `database/logs_writer.py` (Batched insert).
3.  **Memory**:
    *   **Artifacts**: `executor` prevents re-sync.

## 8. Setup, Build, and Execution

### Prerequisites
*   Python Environment (`.venv`).
*   **Environment Variables**:
    *   `API_KEY`: For `/api`.
    *   `AZURE_KEY` / `AZURE_OPENAI_KEY`: Core execution dependency.
    *   `ANYTHINGLLM_API_KEY`: If using LLM Sync.
    *   `CHROME_USER_DATA_DIR`: Default `chrome_profile`.

### Execution
**App Runner (Unicorn):**
```bash
python -m uvicorn app:app --reload --port 8000
```
**Startup Lifecycle:**
1.  Orchestration `run_startup`.
2.  Concurrent: Mount Artifacts, Cleanup Chrome (Zombie), Init DB.
3.  Sequential: Migrations, Recoveries.
4.  Application: Load Registry, Warmup Browser.

## 9. Known Limitations & Non-Goals

*   **Testing**: 2 Short E2E tests exist (`tests/test_browser_e2e.py`). No unit tests for `scraper` or `engine`.
*   **Language**: Strictly English (Logs/Prompts).
*   **PDF/Vector**: Legacy `deprecated` only. Not part of active `base.py` loading.
*   **CDP Health**: The "45s Sleep" is a hack to avoid extension collision. `wait=2` on element presence.
*   **Artifacts**: FIFO subject to orphaning (manual checks required).