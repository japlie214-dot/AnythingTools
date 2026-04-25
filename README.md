# AnythingTools - Deterministic Tool Hosting Service

## 1. Project Overview

AnythingTools is a small, deterministic tool-hosting service that exposes a fixed set of tools via an HTTP API. It runs tools in threads, serializes all writes through a background writer to a SQLite database in WAL mode, and delivers structured markdown callbacks to an external service (AnythingLLM) with a durable retry mechanism. The repository implements a web-content Scraper with strict validation and DOM pre-checks, a Draft Editor, a Batch Reader (semantic search over batches), and a Publisher (Telegram) with automated resumption and crash guards.

This README documents the codebase as it exists after the PLAN-01 security and resilience remediation. Every statement below is based on explicit code, comments, log tags, and configuration found in the repository at the time of writing.

**Key Recent Changes (PLAN-01):**
- Scraper enhancements: Video/audio rejection, DOM pre-checks, Phase 0 ULID validation
- Publisher overhaul: Resume field removal, automatic resumption, `_is_article()` sanitization
- Pipeline resilience: Traceback guards, path normalization, dynamic truncation, error injection

---

## 2. High-Level Architecture

- **API**: FastAPI in `app.py` exposes endpoints to enqueue tools, read job status, and cancel jobs.
- **Worker Manager**: A single `UnifiedWorkerManager` in `bot/engine/worker.py` polls the DB, claims jobs, and spawns threads to execute tools.
- **Tools**: Each tool implements a `BaseTool` pattern in `tools/*/tool.py` and emits structured payloads to be sent as callbacks.
- **Single Writer**: All mutations use `enqueue_write()` to a background writer (`database/writer.py`) ensuring single-writer semantics with SQLite.
- **Artifacts**: Files are persisted under the configured AnythingLLM artifacts directory using `utils/artifact_manager.py`.
- **Callbacks**: Worker constructs a markdown callback using `utils/callback_helper.py` and delivers it to AnythingLLM using an HTTP client with exponential backoff.

Execution model: event-driven polling (1s poll interval). Tools run in threads for isolation but share the same database and artifact directories.

---

## 3. Repository Layout

**Top-level directories:**

- `api/` — FastAPI routes and input schemas. Enhanced 422 formatting with field-level errors.
- `bot/` — Engine and runner. `bot/engine/worker.py` contains the UnifiedWorkerManager.
- `clients/` — External service adapters (LLM providers, Snowflake).
- `database/` — Connection management, background writer, migrations, schemas (BASE_SCHEMA_VERSION = 6).
- `deprecated/` — Legacy code and obsolete implementations (scraper research, finance tools, browser actions).
- `tools/` — Tool implementations:
  - `scraper/` — Complete pipeline: extraction, curation (Top10Curator), persistence, paywall detection, validation prompts
  - `publisher/` — Telegram delivery pipeline with state management
  - `batch_reader/` — Hybrid semantic search over artifact batches
  - `draft_editor/` — Content editing tools
- `utils/` — Infrastructure helpers: artifact manager, callback formatter, hybrid search, logger, Telegram client.

**Key modified files for PLAN-01:**
- `tools/scraper/scraper_prompts.py` - Updated validation prompt
- `tools/scraper/extraction.py` - Added DOM pre-checks
- `tools/scraper/curation.py` - Phase 0 ULID validation
- `tools/scraper/tool.py` - Job status log timing
- `tools/publisher/tool.py` - Resume removal and automatic resumption
- `utils/telegram/publisher.py` - URL fallbacks
- `utils/telegram/pipeline.py` - Traceback guards
- `utils/callback_helper.py` - Path normalization and dynamic truncation
- `bot/engine/worker.py` - Path resolution
- `config.py` - New constants

---

## 4. Core Concepts & Domain Model

### 4.1 Jobs and State

- **Jobs are canonical in the database**; in-memory state is ephemeral
- All mutations must use `enqueue_write()` to serialize through the single-writer background thread
- Job lifecycle: `QUEUED` → `RUNNING` → `COMPLETED|PARTIAL|FAILED|PENDING_CALLBACK|CANCELLING`
- `PENDING_CALLBACK` triggers automatic retry delivery in worker

### 4.2 Artifacts

- Stored under `ANYTHINGLLM_ARTIFACTS_DIR/tool/{job_or_batch_id}/`
- Created using `write_artifact()` from `utils/artifact_manager.py`
- **Manifest files** are written synchronously with detailed directory index format before job finalization
- Callbacks include `artifacts` metadata and `artifacts_directory` path (now resolved to absolute paths)

### 4.3 Input Validation & Safety

- **Input models** use Pydantic for validation with enhanced error reporting
- **ULIDs** are used for all job IDs and article IDs (8-byte truncated for SQLite integer fit)
- **Resume field removal**: Publisher no longer accepts manual `resume` parameter; it auto-detects state
- **Article sanitization**: `_is_article()` enforces `status=SUCCESS`, `ulid`, `title`, `conclusion`, `url`

### 4.4 Video/Audio Content Rejection

- **DOM pre-checks** in `tools/scraper/extraction.py`:
  - Detects `<video>`, `<audio>` tags
  - Detects video platform embeds (YouTube, Vimeo, Dailymotion) via iframe src
  - Rejects if paragraph text < 500 chars
- **Updated validation prompt** now explicitly rejects video/audio-only pages

### 4.5 Context Budget Management

- **LLM_CONTEXT_CHAR_LIMIT**: Configurable base limit (default 40000)
- **CALLBACK_TRUNCATION_MULTIPLIER**: Factor for callback truncation (default 0.5)
- **Packing multiplier**: 0.8 in curation.py and persistence.py for context budget
- **Truncation logic**: Enforces dynamic limit instead of hardcoded 12000 chars

---

## 5. Detailed Behavior

### 5.1 Scraper Pipeline

1. **Job Creation**: `POST /api/tools/scraper` creates batch with PENDING status
2. **Browser Launch**: Headful browser via Botasaurus
3. **Link Extraction**: Deduplication and filtering
4. **Per-Article Processing**:
   - **Paywall Detection**: 3 retry attempts with auto-refresh
   - **DOM Pre-Check**: Video/audio detection + paragraph threshold
   - **LLM Validation**: Uses `VALIDATION_PROMPT` with JSON response
   - **Summarization**: JSON schema → JSON object fallback
   - **Embedding**: Snowflake or sqlite-vec
5. **Curation**: Top10Curator packs to 80% budget, scores quality, retries 3x
6. **Artifacts**: Manifest + raw/curated JSON + callbacks
7. **Finalization**: Status logged AFTER all artifacts persisted

### 5.2 Publisher Pipeline

1. **Input**: `batch_id` (+ optional `finalize` flag)
2. **State Detection**: Reads `broadcast_batches.status` 
3. **Automatic Resumption**: If status is `PENDING` or `PARTIAL`, runs with `resume=True`
4. **Finalization**: If `finalize=True` and status is `PARTIAL`, sets to `COMPLETED` without publishing
5. **Validation**: Uses `_is_article()` to filter invalid entries
6. **Telegram Delivery**:
   - Briefing to configured chat
   - Archive to separate chat
   - URL fallback: "URL Unavailable" if missing
   - Crash guards: Exception handling with traceback logging

### 5.3 Callback System

- **Format**: Structured markdown with header, summary, artifacts, status definitions
- **Truncation**: `truncate_message()` uses dynamic: `base_limit * multiplier` (default 20000 chars)
- **Error Injection**: For FAILED status, queries last 10 errors from `job_logs` and appends
- **Path Normalization**: Uses `Path.as_posix()` with absolute path detection to prevent duplication

### 5.4 Crash Recovery

- **Traceback Guards**: Added in `tools/publisher/tool.py` and `utils/telegram/pipeline.py`
- **STATE REVERSION**: On crash, publisher sets batch to `PARTIAL` (not `FAILED`)
- **Automatic Rerun**: Worker can re-claim `PARTIAL` batches without manual intervention

---

## 6. Public Interfaces

### API Endpoints

- `POST /api/tools/{tool_name}` — Enqueue job (enhanced validation with 422 schema)
- `GET /api/jobs/{job_id}` — Status, logs, payload
- `DELETE /api/jobs/{job_id}` — CANCELLING signal
- `GET /api/manifest` — Tool registry

### Tool Input Models

**Scraper Input:**
```json
{"target_site": "string"}  // Valid options: Bloomberg, Reuters, etc.
```

**Publisher Input:**
```json
{
  "batch_id": "string",      // Required ULID
  "reset": false,            // Force full reset (optional)
  "finalize": false          // Mark PARTIAL as COMPLETED (optional)
}
// Note: 'resume' field removed - automatic resumption only
```

**Batch Reader Input:**
```json
{
  "batch_id": "string",
  "query": "semantic search query"
}
```

### Callback Payloads

Structure (always):
```json
{
  "_callback_format": "structured",
  "tool_name": "string",
  "status": "COMPLETED|PARTIAL|FAILED",
  "summary": "markdown string",
  "details": { /* tool-specific */ },
  "artifacts": [ /* array */ ],
  "status_overrides": { /* optional */ }
}
```

---

## 7. State, Persistence, and Data

### Database Schema (Base v6)

**Tables:**
- `jobs` — job metadata, status, payload
- `job_logs` — timestamped events from all components
- `broadcast_batches` — scraper batch results (raw/curated paths, status, phase_state)
- `scraped_articles` — article content + metadata
- `scraped_articles_vec` — embeddings

**Write Path:**
1. Component calls `enqueue_write(sql, params)`
2. Background writer thread (`database/writer.py`) executes sequentially
3. WAL mode ensures consistency

### Artifacts Format

**Scraper Manifest** (professional directory index):
```
# Tool: scraper
Timestamp: 2026-04-25T...
Batch ID: {batch_id}
Input Parameters: {"target_site": "..."}

## Output Files
- top10.json (json) - Curated top 10 articles
- raw.json (json) - All scraped articles
- manifest.md (md) - Directory index
- [additional artifacts...]
```

**Phase State** (for Publisher resumption):
- JSON stored in `broadcast_batches.phase_state`
- Tracks per-article: `publish_briefing`, `publish_archive` status

---

## 8. Dependencies & Integration

### Core Dependencies

**Runtime:**
- FastAPI + Uvicorn (API)
- Botasaurus + Selenium (browser automation)
- httpx (callbacks)
- Pydantic (validation)
- python-telegram-bot (messaging)
- sqlite3 + sqlite-vec (storage)

**Async/Sync:**
- Thread-based tools (work isolation)
- Background writer thread (single-writer DB)

### Environment Variables

**Required:**
- `DATABASE_PATH` - SQLite WAL file
- `ANYTHINGLLM_ARTIFACTS_DIR` - Artifact root
- `ANYTHINGLLM_BASE_URL` - Callback destination
- `ANYTHINGLLM_API_KEY` - Auth

**LLM/AI:**
- `AZURE_KEY` / `AZURE_ENDPOINT` / `AZURE_DEPLOYMENT`
- `CHUTES_API_TOKEN`

**Telegram:**
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_BRIEFING_CHAT_ID`
- `TELEGRAM_ARCHIVE_CHAT_ID`

**Configurable Limits:**
- `LLM_CONTEXT_CHAR_LIMIT` (default 40000)
- `CALLBACK_TRUNCATION_MULTIPLIER` (default 0.5)

---

## 9. Setup, Build, and Execution

### Prerequisites
- Python 3.10+
- Chrome browser (for Botasaurus)
- SQLite 3.35+ (WAL mode)

### Installation
```bash
# Clone repo
git clone <repo>/AnythingTools
cd AnythingTools

# Install dependencies
pip install -r requirements.txt

# Environment setup
cp .env.example .env
# Edit .env with your credentials

# Database initialization (automatic on first run)
# Migrations are applied automatically
```

### Running
```bash
# Start API
python app.py
# Or: uvicorn app:app --host 0.0.0.0 --port 8000

# Worker runs automatically in background thread
# No separate command needed
```

### Testing
```bash
# Run tests (if present)
pytest tests/  # though current repo appears to lack test suite
```

---

## 10. Testing & Validation

### Current Coverage
**Observed gaps:**
- No `tests/` directory in repository
- No `pytest`, `unittest`, or `coverage` in requirements
- No CI configuration files observed

**Manual validation methods:**
1. Use API endpoints to enqueue jobs
2. Monitor `job_logs` table for execution flow
3. Check artifact directory for generated files
4. Verify callback delivery via external monitoring

### Testing Areas Needed
- Scraper DOM pre-checks (video/audio rejection)
- Publisher automatic resumption
- Callback truncation with dynamic limits
- Crash recovery path reversion
- Path normalization edge cases

---

## 11. Known Limitations & Non-Goals

### Technical Debt
- **Vestigial modules**: `deprecated/` contains ~70% of repo volume; clutter in discovery
- **Firestore dependency**: Found in code but not exercised; unclear if functional
- **No test suite**: Core business logic lacks automated verification

### Hard Constraints (Evidence-based)
- **Single browser**: Botasaurus runs one browser instance per process
- **SKU rate limits**: Callbacks retry only 3x; permanent 4xx failures not retried
- **Memory pressure**: Articles stored in-memory in dicts before batch write; no streaming
- **LLM dependence**: Validation/summary require working LLM endpoint; no offline fallback

### Misleading Implied Features (Not Implemented)
- **Multiple scraper targets**: Only one target at a time; batch ID per run
- **Live updates**: No websocket/event streaming; polling only
- **Granular permissions**: No auth beyond API key; user-level isolation absent
- **Resume from crash**: Publisher handles this; Scraper does not (would restart from beginning)

### Explicit Non-Goals
- **Multi-tenant**: Single artifact root, single DB
- **Real-time sync**: Batch-oriented only
- **Rich UI**: Minimal interface; CLI/HTTP only

---

## 12. Change Sensitivity

### Extremely Fragile Components

1. **`utils/callback_helper.py`**
   - Must preserve `0.5` multiplier for callbacks
   - `truncate_message()` signature impacts all tools
   - Change requires coordinated updates in `worker.py`

2. **`tools/scraper/extraction.py`**
   - Video/audio detection regex patterns: brittle to site changes
   - 500-char threshold: impacts validation rates
   - Paywall detector: integration with `PaywallDetector().detect()` is sensitive

3. **`bot/engine/worker.py`**
   - Polling interval: hard-coded 1s
   - 3-attempt backoff: impacts callback success rates
   - Thread pool: no backpressure limits visible

### Tightly Coupled Areas

- **Database schema**: Any change affects migrations across 6 versions
- **Artifact manager**: Path construction affects aggregator output
- **Input models**: Pydantic models propagate through API validation

### Easy Extension Points

- **New tools**: Add `tools/{name}/tool.py` + register in registry
- **Prompt tuning**: `scraper_prompts.py` contains all LLM interactions
- **Publisher destinations**: `utils/telegram/` can be cloned for Slack/Discord

### Hardest Refactors

- **Replace Botasaurus**: Browser automation is deeply embedded
- **Split artifact root**: Requires changes in multiple path builders
- **Async rewrite**: Thread-based model would require major restructuring