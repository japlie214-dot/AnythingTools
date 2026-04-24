# AnythingTools - Deterministic Tool Hosting Service

## 1. Project Overview

AnythingTools is a small, deterministic tool-hosting service that exposes a fixed set of tools via an HTTP API. It runs tools in threads, serializes all writes through a background writer to a SQLite database in WAL mode, and delivers structured markdown callbacks to an external service (AnythingLLM) with a durable retry mechanism. The repository contains implementations for a web-content Scraper, a Draft Editor, a Batch Reader (semantic search over batches), and a Publisher (Telegram).

This README documents the codebase as it exists in this workspace at the time of writing. Every statement below is based on explicit code, comments, log tags, and configuration found in the repository.

Important modified files referenced in this README:
- [`tools/scraper/extraction.py`](tools/scraper/extraction.py:117)
- [`tools/scraper/curation.py`](tools/scraper/curation.py:82)
- [`tools/scraper/tool.py`](tools/scraper/tool.py:250)
- [`utils/callback_helper.py`](utils/callback_helper.py:102)

---

## 2. High-Level Architecture

- API: FastAPI in [`app.py`](app.py:1) exposes endpoints to enqueue tools, read job status, and cancel jobs.
- Worker Manager: A single `UnifiedWorkerManager` in [`bot/engine/worker.py`](bot/engine/worker.py:160) polls the DB, claims jobs, and spawns threads to execute tools.
- Tools: Each tool implements a `BaseTool` pattern in `tools/*/tool.py` and emits structured payloads to be sent as callbacks.
- Single Writer: All mutations use `enqueue_write()` to a background writer (`database/writer.py`) ensuring single-writer semantics with SQLite.
- Artifacts: Files are persisted under the configured AnythingLLM artifacts directory using [`utils/artifact_manager.py`](utils/artifact_manager.py:1).
- Callbacks: Worker constructs a markdown callback using [`utils/callback_helper.py`](utils/callback_helper.py:102) and delivers it to AnythingLLM using an HTTP client with exponential backoff.

Execution model: event-driven polling (1s poll interval). Tools run in threads for isolation but share the same database and artifact directories.

---

## 3. Repository Layout (top-level important directories)

- `api/` — routes and input schemas. See [`api/routes.py`](api/routes.py:44) for enqueueing and enhanced 422 formatting.
- `bot/` — engine and runner. See [`bot/engine/worker.py`](bot/engine/worker.py:160).
- `clients/` — external service adapters (LLM, Snowflake).
- `database/` — connection, writer, migrations, schemas (BASE_SCHEMA_VERSION = 6).
- `tools/` — tool implementations. Key subfolders:
  - `tools/scraper/` — Scraper pipeline (extraction, curation, persistence, tool wrapper).
  - `tools/publisher/` — Telegram publishing pipeline.
  - `tools/batch_reader/` — batch-based hybrid semantic search.
- `utils/` — helpers: artifact manager, callback formatter, hybrid search, loggers.

Files modified to implement recent fixes are explicitly noted in this README and correspond to applied edits in the workspace.

---

## 4. Core Concepts and Key Invariants

- Jobs are canonical in the DB; in-memory state is ephemeral. All writes must go through `enqueue_write()`.
- Artifacts are stored on disk under a configured `ANYTHINGLLM_ARTIFACTS_DIR`, in tool-specific subdirectories `{tool}/{job_or_batch_id}/`.
- Tools must emit a structured payload (key `_callback_format: "structured"`) for the worker to build proper callbacks.
- Callback delivery is retried at the worker level: up to 3 attempts with exponential backoff; permanent client-side 4xx errors are not retried.
- Scraper manifest generation and artifact writes are synchronous in the tool so the final state is persisted before the final job status is logged.

---

## 5. Detailed Behavior — Scraper Focus (current code)

This section is narrowly focused on the Scraper pipeline because that is where recent changes were applied.

### 5.1 Overall Flow (scraper)

1. `POST /api/tools/scraper` creates a `jobs` row (QUEUED). See [`api/routes.py`](api/routes.py:44).
2. Worker claims the job and runs the tool instance (thread).
3. Scraper launches a headful browser via [`utils/browser_daemon.py`](utils/browser_daemon.py:1) and runs `_run_botasaurus_scraper()` / `process_article()` flows.
4. For each article: paywall detection → validation (LLM) → summarization (LLM with JSON schema fallback) → embedding (Snowflake or sqlite-vec fallback).
5. Curation picks the Top-N (default ~10) using `Top10Curator`.
6. Artifacts are written via `write_artifact()` into `{ANYTHINGLLM_ARTIFACTS_DIR}/scraper/{batch_id}/`.
7. A manifest file is created and written as an artifact, and `broadcast_batches` is updated.
8. The tool emits a structured payload which the worker converts to a markdown callback and POSTs to AnythingLLM. Callback delivery success updates job status to COMPLETED; failure flips to PENDING_CALLBACK and the DB-driven retry flow takes over.

### 5.2 Paywall / Gate Handling (applied)

- Location: [`tools/scraper/extraction.py`](tools/scraper/extraction.py:166).
- Behavior now in code: paywall detection is attempted after HTML capture. If `PaywallDetector().detect()` identifies a paywall, the tool logs a `Scraper:Paywall` warning with attempt count. The logic then performs an auto-refresh loop up to 3 attempts. The exact behavior in the source is:
  - On paywall detection: log `Scraper:Paywall` with attempt
  - If `val_attempt < 3`: the loop `continue`s to the top so the page is reloaded and validation retried
  - After 3 attempts, returns `{"status": "FAILED", "reason": "Paywall persists after 3 retries..."}`
- Evidence: see [`tools/scraper/extraction.py`](tools/scraper/extraction.py:166).

This implements the requirement to attempt up to three auto-refresh attempts on paywall detection.

### 5.3 Validation Loop and Summarization

- Validation uses an LLM prompt (`VALIDATION_PROMPT`) and the result is parsed by `parse_llm_json()`; the raw response is logged to `Scraper:Validation:Response` (includes `raw` and `parsed` payload).
- If validation fails and is retriable (per paywall logic), the code will log the attempt and retry (see above). If validation fails non-recoverably or after retries, the article is marked FAILED.
- Summarization: attempts JSON schema (`json_schema`) first; on `BadRequestError` falls back to `json_object`. Context-length errors raise immediately.

### 5.4 Curation — Observability and Scoring (applied)

- Location: [`tools/scraper/curation.py`](tools/scraper/curation.py:82).
- The `Top10Curator` class: packs context up to 80% of `LLM_CONTEXT_CHAR_LIMIT`, produces a dynamic `target_count`, queries an LLM to choose exactly that many ULIDs, and retries up to 3 times.
- Observability improvements added in the current workspace:
  - `LLM:Azure:Request` log before calling the LLM and `LLM:Azure:Response` immediately after receiving the response (payload includes attempt and `batch_id`-style context if provided).
  - After parsing candidate ULIDs each attempt, the code calls `_score_curation_quality()` and logs the composite quality via `Scraper:Curation:Score` with the quality breakdown.
  - When a perfect result is selected, the code logs `Scraper:Curation:Selected` and returns immediately.
  - On retry exhaustion, the code logs `Scraper:Curation:Exhausted` and returns the best scoring partial result (if any); a final `Scraper:Curation:Selected` log is emitted indicating whether a partial or fallback result was used.
- The scoring function is implemented and returns a dict containing `composite`, `coverage_ratio`, `uniqueness_score`, `validity_score`, `diversity_score`, `item_count`, and `target_count`.
- Evidence: see [`tools/scraper/curation.py`](tools/scraper/curation.py:119).

### 5.5 Manifest / Directory Index (applied)

- The `manifest.md` produced by the scraper has been re-engineered (in the current workspace) into a professional directory index rather than a narrative briefing.
- Location: [`tools/scraper/tool.py`](tools/scraper/tool.py:250).
- Format produced by current code (directly written as the manifest artifact):
  - Header with Tool Name (job tool), Timestamp (ISO), Batch ID, and Input Parameters (JSON snippet)
  - A structured "Output Files" list enumerating produced artifact filenames with type and descriptions
- The manifest is written synchronously before the tool finalizes status. The code attempts to write the manifest artifact and records it in the artifact accumulator before it sets the terminal job status. This satisfies the rule that finalization and the `Scraper:Job:Status` log occur after artifacts and the manifest are persisted.
- Evidence: see [`tools/scraper/tool.py`](tools/scraper/tool.py:290-301).

### 5.6 Callback Summary and Finalization Timing (applied)

- The tool now constructs a `callback_summary_markdown` that contains a curated content preview (Top list + Next 50 preview). This markdown is placed into the `summary` field of the structured payload.
- The code updates job final status only after the manifest has been persisted and `broadcast_batches` has been updated; a final `Scraper:Job:Status` log entry is emitted after persistence.
- Evidence: [`tools/scraper/tool.py`](tools/scraper/tool.py:320-370).

### 5.7 Callback Artifact Path and JSON Details (partial / pending)

- Current `utils/callback_helper.py` formats artifacts lists using the `artifacts_subdir` parameter and renders a Details JSON block for `details` (see [`utils/callback_helper.py`](utils/callback_helper.py:102)).
- PLAN-03 requested two further changes which are **not applied** in the current workspace and therefore are still pending:
  1. Prepending `ANYTHINGLLM_ARTIFACTS_DIR` to the `artifacts_subdir` to produce absolute paths in callback output (not present in current code).
  2. Removing the JSON "Details" block entirely from the callback markdown (current code still renders `details` as a JSON code fence).
- Evidence: see [`utils/callback_helper.py`](utils/callback_helper.py:112-119).

These two items remain unimplemented in the current checkout and are explicitly documented as pending (they are not guessed; they are absent from the code).

---

## 6. Public Interfaces (unchanged)

- `POST /api/tools/{tool_name}` — enqueues a tool run. Validation uses tool `INPUT_MODEL` when present; enhanced 422 responses provide field-level errors and an `expected_schema` when available (implemented in [`api/routes.py`](api/routes.py:70)).
- `GET /api/jobs/{job_id}` — returns status, job_logs, and last payload if any.
- `DELETE /api/jobs/{job_id}` — marks a job CANCELLING and sets the cancellation flag in the running manager.
- `GET /api/manifest` — returns the registry schema for the available tools.

All endpoint behavior is implemented in `api/routes.py` and uses `enqueue_write()` for mutations.

---

## 7. Persistence & Artifacts

- Artifacts are written by `write_artifact()` under a path formed from `ANYTHINGLLM_ARTIFACTS_DIR`, `<tool>`, and a job/batch id.
- Current artifact path pattern used by `write_artifact()` in the code is `{ANYTHINGLLM_ARTIFACTS_DIR}/{tool}/{job_id}/` (see `utils/artifact_manager.py`). The callbacks produced by the worker include `artifacts` metadata and an `artifacts_directory` field pointing at `scraper/{safe_job_id}` (relative) in the current implementation.
- The manifest artifact is written synchronously and included in the recorded artifacts list prior to job finalization.

---

## 8. Dependencies & Environment

The dependency set is declared in `requirements.txt`. The runtime integrations used in this codebase include:
- FastAPI, Uvicorn — API and server
- Botasaurus + selenium — browser automation for scraping
- httpx — callback delivery
- pydantic — input validation
- sqlite3 / sqlite-vec — persistent data and fallback vector storage
- Azure OpenAI integration inside `clients/llm` helpers
- python-telegram-bot — Telegram publishing

Required environment variables include `DATABASE_PATH`, `ANYTHINGLLM_ARTIFACTS_DIR`, `ANYTHINGLLM_CALLBACK_URL`, Azure and Snowflake credentials, and Telegram tokens. See `config.py` for exact names.