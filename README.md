# AnythingTools - Deterministic Tool Hosting Service

## 1. Project Overview

**AnythingTools** is a deterministic tool-hosting service that executes exactly four whitelisted tools via HTTP API. The system operates as a direct execution engine with robust state management, automatic database recovery, and resume-capable pipelines.

### What It Does
- Provides REST API endpoints for four tools: `scraper`, `draft_editor`, `batch_reader`, and `publisher`
- Executes tools in isolated threads with single-writer SQLite database guarantees
- Scrapes web content with Botasaurus, generates embeddings via Snowflake, and publishes translated content to Telegram channels
- Maintains granular job state with automatic resume capability after interruptions
- Automatically manages database schema migrations with auto-folding mechanism
- Implements hybrid search combining SQLite FTS5 keyword indexing and vector embeddings using Application-Layer Reciprocal Rank Fusion (RRF)
- **Robust callback system**: Atomic HTTP callbacks to AnythingLLM with durable logging, PENDING_CALLBACK state, and database-driven retry (max 3 attempts)
- **Structured AI-actionable callbacks**: Every tool exit path emits standardized markdown with explicit status_overrides and recovery instructions
- **Custom-documents directive**: Artifacts written directly to AnythingLLM's custom-documents folder, NO Base64 attachments via Chat API

### What It Does NOT Do
- Does not execute autonomous agent loops or reasoning chains
- Does not dynamically discover or load tools beyond the hardcoded whitelist
- Does not support concurrent browser operations (single browser lock)
- Does not provide real-time streaming responses
- Does not offer multi-tenancy or user isolation
- Does not implement dynamic tool loading or runtime tool discovery
- Does not support direct Base64 attachment uploads (architectural constraint)

---

## 2. High-Level Architecture

### 2.1 Data Flow
```
API Request → Job Queue (QUEUED) → Worker Poller → Tool Execution
  ↓
  ├─ Success → _do_callback_with_logging()
  │            ├─ HTTP 2xx → COMPLETED
  │            └─ HTTP error → PENDING_CALLBACK → Database Retry Loop (max 3) → PARTIAL
  └─ Tool Failure → FAILED
```

### 2.2 Runtime Model
**Event-driven polling**: Polling interval 1 second in `bot/engine/worker.py`  
**Execution model**: Direct tool invocation, no autonomous loops  
**Concurrency**: Single-writer database, thread-based job execution  
**Architecture**: FastAPI + Background Writer Thread + Worker Manager

### 2.3 System Components

```
┌─────────────────────────────────────────────────────────────┐
│                       Entry Point (app.py)                   │
│  • FastAPI lifespan hooks                                   │
│  • Static file mounting (artifacts/)                        │
│  • Schema initialization (fast-path for fresh DBs)          │
│  • Writer thread startup (single-writer guarantee)          │
└──────────────────┬──────────────────────────────────────────┘
                         │
                         ├──────────────┬──────────────┬──────────────┐
                         │              │              │              │
                  ┌──────▼──────┐  ┌────▼──────┐  ┌────▼──────┐  ┌────▼──────┐
                  │ API Routes  │  │  Writer   │  │  Worker   │  │  Tools    │
                  │ /api/tools  │  │  Thread   │  │  Manager  │  │  Registry │
                  │ /api/jobs   │  └────┬──────┘  └────┬──────┘  └────┬──────┘
                  └─────────────┘       │              │              │
                                        │              │              │
                                   ┌────▼──────┐   ┌───▼────┐
                                   │  SQLite   │   │ Tools  │
                                   │  WAL DB   │   │ Scraper│
                                   │           │   │ etc.   │
                                   └───────────┘   └────────┘
```

---

## 3. Repository Structure

### 3.1 Root Directory
- **`app.py`** - FastAPI entrypoint with lifespan lifecycle, startup validation for `ANYTHINGLLM_ARTIFACTS_DIR`, temp directory purging
- **`config.py`** - Configuration reading from environment variables
- **`requirements.txt`** - Dependencies including Botasaurus, Snowflake, PaddleOCR
- **`.env`** - Environment variables (Telegram credentials, schema reset flag)

### 3.2 Database Layer (`database/`)

#### Core Modules:
- **`connection.py`** - `DatabaseManager` with thread-local connections, WAL mode, sqlite_vec detection
- **`writer.py`** - Background writer thread with queue:
  - Auto-repair logic (1 retry for missing tables)
  - `EXEC_SCRIPT` marker for batch script execution
  - `TRANSACTION_MARKER` for atomic transaction bundles
  - `enqueue_transaction()` for dual-table updates
- **`health.py`** - Isolated health checks, orphaned backup recovery
- **`lifecycle.py`** - Async coordinator for initialization and migration
- **`schema.py`** - Proxy layer to schemas and migrations
- **`job_queue.py`** - Job operations with JSON metadata
- **`reader.py`** - Read operations with JSON extraction
- **`blackboard.py`** - State tracking using JSON metadata

#### Migration System (`database/migrations/`):
- **`__init__.py`** - Autonomous runner with:
  - Monotonically increasing version validation
  - Transaction safety (`BEGIN EXCLUSIVE`)
  - Backup/restore on failure
  - Version alignment with BASE_SCHEMA_VERSION
  - Future-version detection prevents downgrade
  - Auto-fold mechanism when migrations exceed 3 files
- **`v004_step_to_metadata.py`** - Converts `step_identifier` → `item_metadata` JSON
- **`v005_jobs_partial.py`** - Adds `PARTIAL` status to jobs
- **`v006_publisher_phase_state.py`** - Migrates `posted_*_ulids` → `phase_state` JSON
- **`v007_fts5_hybrid.py`** - Creates FTS5 virtual table, triggers, and `vec_rowid` index
- **`v008_pending_callback.py`** - Adds `PENDING_CALLBACK` status and `retry_count` to jobs

#### Schema Registry (`database/schemas/`):
- **`__init__.py`** - Domain registry pattern, `BASE_SCHEMA_VERSION = 6`, `MAX_MIGRATION_SCRIPTS = 3`
- **`jobs.py`** - Jobs, job_items (with `item_metadata` JSON), job_logs, broadcast_batches (with `phase_state` JSON)
- **`finance.py`** - Financial tables (unused in current pipeline)
- **`vector.py`** - Vector tables with sqlite-vec fallback, includes `scraped_articles_fts` and triggers
- **`pdf.py`** - PDF parsing tables
- **`token.py`** - Token usage tracking

#### Migration Archive (`database/migrations_archive/`):
- Stores folded migrations for historical reference

### 3.3 Tools (`tools/`)

#### Registry & Base:
- **`registry.py`** - Whitelist enforcement (4 tools only), dynamic loading, manifest generation
- **`base.py`** - `BaseTool` abstract class

#### Active Tools:
**Scraper (`tools/scraper/`):**
- **`tool.py`** - Scout Mode, Botasaurus integration, Intelligent Manifest generation, structured callbacks
- **`task.py`** - Botasaurus scraper implementation
- **`prompt.py`** - Scraping prompts
- **`scraper_prompts.py`** - Contains `SUMMARIZATION_SCHEMA` with anyOf syntax for nullable `error` field
- **`summary_prompts.py`** - Summarization prompts
- **`targets.py`** - Valid target site configuration
- **`extraction.py`** - JSON schema attempt with fallback to `json_object`, hardened exception handling
- **`persistence.py`** - Structured JSON parsing with null handling
- **`curation.py`** - **NEW**: Smart context packing with knapsack algorithm, validated curation with 3-retry loop
- **Resume behavior**: Skips if both validation and summary exist in job_items
- **Structured output**: Returns `_callback_format: "structured"` with status_overrides

**Draft Editor (`tools/draft_editor/`):**
- **`tool.py`** - Atomic SWAP operations, PENDING status lock, structured callbacks with failure modes

**Batch Reader (`tools/batch_reader/`):**
- **`tool.py`** - Semantic search using hybrid RRF, filtered by batch_id, structured callbacks
- **New Hybrid Search**: Uses `utils/hybrid_search.py` for vector + keyword fusion

**Publisher (`tools/publisher/`):**
- **`tool.py`** - Orchestrates `utils.telegram.pipeline.PublisherPipeline`, structured callbacks
- **`Skill.py`** - Skill wrapper
- **`prompt.py`** - Contains `TRANSLATION_PROMPT` with strict MarkdownV2 rules (raw string literal)

### 3.4 Execution Layer (`bot/`)

#### Engine (`bot/engine/`):
- **`worker.py`** - `UnifiedWorkerManager` with 1-second polling loop
  - Polls jobs prioritizing `INTERRUPTED`
  - Spawns execution threads
  - Crash recovery (3 strikes → `ABANDONED`)
  - AnythingLLM callback on `COMPLETED`/`PARTIAL`
  - **Structured callback construction**: Uses `format_callback_message()` from `callback_helper`
  - **Exponential backoff**: Base 2s, attempts 3 times
  - **Custom-documents enforcement**: `attachments: []` always empty
- **`tool_runner.py`** - `run_tool_safely` wrapper with timeout

### 3.5 API Layer (`api/`)
- **`routes.py`** - Endpoints:
  - `POST /api/tools/{tool_name}` - Enqueue job (202)
  - `GET /api/jobs/{job_id}` - Status + logs
  - `DELETE /api/jobs/{job_id}` - Cancellation request
  - `GET /api/manifest` - Tool schemas
  - `GET /api/metrics` - System metrics
- **`schemas.py`** - Pydantic models for input validation

### 3.6 Utilities (`utils/`)

#### Core Callback Infrastructure:
- **`callback_helper.py`** - Standardized callback formatting:
  - `CallbackStatus` enum: COMPLETED, PARTIAL, FAILED, PENDING_CALLBACK, CANCELLING
  - `StatusDefinition` dataclass with descriptions and next_steps
  - `format_callback_message()` - Constructs markdown with header, summary, details, artifacts, status_overrides
  - `truncate_message()` - Prevents callback payloads exceeding 12k chars
  - `format_artifacts_list()` - Renders artifact table for LLM consumption

#### Custom Documents Enforcement:
- **`artifact_manager.py`** - Artifact persistence to AnythingLLM custom-documents:
  - `get_artifacts_root()` - Validates `ANYTHINGLLM_ARTIFACTS_DIR` exists
  - `write_artifact()` - Creates nested subdirectories: `{ANYTHINGLLM_ARTIFACTS_DIR}/{tool_name}/{job_id}/`
  - `artifact_url_from_request()` - Constructs public URLs for API responses
  - **Atomic writes**: Temp file + rename pattern for crash safety

#### Telegram Package (Modular Architecture):
- **`__init__.py`** - Exports all classes
- **`types.py`** - `TelegramErrorInfo`, `PhaseState` dataclasses
- **`rate_limiter.py`** - Global `threading.Lock`-based rate limiter with wait-and-block strategy
- **`telegram_client.py`** - PTB 22.7 compliant async client
- **`state_manager.py`** - Atomic state + DB updates via `enqueue_transaction()`
- **`validator.py`** - Article validation with job logging
- **`translator.py`** - LLM batch translation with caching/retry
- **`publisher.py`** - Channel delivery with atomic phase-state updates
- **`pipeline.py`** - Orchestrator with exact parity resume logic

#### Core Utilities:
- **`text_processing.py`** - **Updated `escape_markdown_v2()`** with two critical bug fixes:
  1. Character range: Fixed regex from `[\\_*\[\]()~`>#+\-=|{}.!]` to `[\\_*\[\]()~`>#+=|{}.!\-]`
  2. Double-backslash: Fixed replacement from `r'\\\\\1'` to `r'\\\1'`
  - Entity-aware regex: Preserves code blocks, links, spoilers, bold/italic/strikethrough
  - Selective escaping: Only plaintext segments get escaped
- **`browser_lock.py**` - `threading.Lock` for browser exclusivity
- **`browser_daemon.py**` - Driver lifecycle management
- **`browser_utils.py**` - Safe navigation utilities
- **`som_utils.py**` - State-of-math synchronization
- **`metadata_helpers.py**` - JSON metadata construction/parsing
- **`vector_search.py**` - Direct Snowflake client calls, SQLite-vec fallback
- **`hybrid_search.py**` - FTS5 sanitization, Weighted RRF, orchestration for hybrid search
- **`tracker.py**` - Ledgers in `data/temp/` (updated from legacy)

#### Logging:
- **`logger/**` - Dual logging (console + file) with structured payloads

### 3.7 Clients (`clients/`)
- **`snowflake_client.py**` - Direct Snowflake connection
- **`llm/`** - Azure OpenAI wrapper:
  - `_build_responses_payload()` maps `json_schema` to flat Azure Responses API format
  - `_apply_common_payload()` remains untouched (Chutes AI preserved)
  - Architectural comments added explaining Azure API structure

### 3.8 Deprecated (`deprecated/`)
- **Legacy architecture evidence** - UnifiedAgent, dynamic tools, unused tool types (`bot/`, `tools/`)
- *Evolutionary marker*: File structure shows migration from monolithic to modular design

### 3.9 Tests (`tests/`)
- **`test_browser_e2e.py`** - Browser health check
- **`test_migration_pipeline.py`** - Migration test outline

---

## 4. Core Concepts & Domain Model

### 4.1 Job Lifecycle State Machine

#### Normal Flow:
```
QUEUED → RUNNING
         ↓
   ┌─── COMPLETED (callback succeeded)
   ↓
   └─── PENDING_CALLBACK (callback failed, retry scheduled)
           ↓ (polling after delay)
   ┌─── COMPLETED (retry succeeded)
   ↓
   └─── PARTIAL (max 3 retries exceeded)
```

#### Failure/Recovery Paths:
```
RUNNING → FAILED (tool execution failed)
         ↓
   INTERRUPTED (worker crash, recover on startup)
         ↓
   PAUSED_FOR_HITL (manual intervention required)
         ↓
   ABANDONED (after 3 consecutive system crashes)
```

**Global Statuses:** `QUEUED`, `RUNNING`, `COMPLETED`, `PARTIAL`, `FAILED`, `CANCELLING`, `INTERRUPTED`, `PAUSED_FOR_HITL`, `PENDING_CALLBACK`, `ABANDONED`

**PENDING_CALLBACK Flow:**
1. Tool execution succeeds, but `_do_callback_with_logging()` returns `False`
2. `_run_job()` sets status = `PENDING_CALLBACK`, `retry_count = 1`, preserves `result_json`
3. Polling loop (every 1s) picks up jobs where `updated_at < now - {delay}`
4. `_retry_callback_only()` runs in thread:
   - Reads `result_json` (contains `result` and `attachment_paths`)
   - Calls `_do_callback_with_logging()` again
   - Success → `COMPLETED`
   - Failure → Increment `retry_count`, check if `>= 3`
     - Yes → `PARTIAL` + log "Max retries exceeded"
     - No → Update `retry_count` and `updated_at`, wait for next poll

### 4.2 Job Items (Granular Tracking)
**Table:** `jobItems` (after v004 migration)
```sql
CREATE TABLE job_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    item_metadata TEXT,  -- JSON string
    status TEXT NOT NULL DEFAULT 'PENDING',
    input_data TEXT,
    output_data TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
)
```

**Metadata Structure (Version 5+):**
```json
{
  "step": "translate|publish_briefing|publish_archive|validate",
  "ulid": "01J8ABC...",
  "retry": 2,
  "timestamp": "2026-04-17T03:45:00.123Z",
  "model": "gpt-5.4-mini",
  "is_top10": true,
  "error": "Timeout after 3 attempts"
}
```

### 4.3 Broadcast Batches (Publisher State)
**Table:** `broadcast_batches` (after v006 migration)
```sql
CREATE TABLE broadcast_batches (
    batch_id TEXT PRIMARY KEY,
    target_site TEXT NOT NULL,
    raw_json_path TEXT NOT NULL,
    curated_json_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING','PUBLISHING','PARTIAL','COMPLETED','FAILED')),
    phase_state TEXT NOT NULL DEFAULT '{}',  -- JSON string after v006
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
```

**Phase State Structure (Version 6+):**
```json
{
  "validate": { "01J8ABC...": {"status": "COMPLETED"} },
  "translate": { "01J8ABC...": {"status": "COMPLETED"} },
  "publish_briefing": { "01J8ABC...": {"status": "COMPLETED"} },
  "publish_archive": { "01J8ABC...": {"status": "COMPLETED"} }
}
```

**Batch Status Logic:**
- `COMPLETED`: 100% of valid items translated AND all briefings and archives posted
- `PARTIAL`: Mixed outcomes
- `FAILED`: All invalid or complete failure

### 4.4 Migration Version Chain
**Base:** `BASE_SCHEMA_VERSION = 6` (in `database/schemas/__init__.py`)

**Active Migrations (as of current):**
1. **v004** - `step_identifier` → `item_metadata` JSON
2. **v005** - Adds `PARTIAL` status to jobs, updates job_items metadata persistence
3. **v006** - Migrates `posted_*_ulids` → `phase_state` JSON
4. **v007** - Creates FTS5 virtual table, triggers, and `vec_rowid` index for hybrid search
5. **v008** - Adds `PENDING_CALLBACK` status to jobs table for callback retry mechanism

**Current DB Version:** 6 (with 4 migration files active, v008 added)

**Auto-Fold Mechanism:**
- If active migrations > 3 (`MAX_MIGRATION_SCRIPTS`), oldest folds into BASE_SCHEMA_VERSION
- All tables re-extracted from memory DB, merged into domain modules
- Migration file deleted from active directory
- Evidence: `database/migrations_archive/` directory

---

## 5. Detailed Behavior & Key Workflows

### 5.1 Scraper Execution Flow

1. **Initialization**
   - Validate target site against `VALID_TARGET_NAMES`
   - Acquire `browser_lock`
   - Get driver from `browser_daemon`

2. **Scraping (via Botasaurus)**
   - Run `_run_botasaurus_scraper()` in thread
   - Per-article: validate → summarize → embed
   - **New**: Uses `json_schema` with fallback to `json_object` for summarization
     - Validates schema syntax with `anyOf` for nullable fields
     - Hardened exception handling: only `BadRequestError` triggers fallback
     - Context-length errors propagate immediately
   - **New**: Parses structured JSON directly (`title`, `conclusion`, `summary` array)
   - **New**: Emits `PARTIAL` status if any embeddings fail
   - Resume check: Skip if `validation_passed` and `summary_generated` exist in `job_items`

3. **Embedding Generation**
   - Direct Snowflake calls: `snowflake_client.embed(text)`
   - Fallback: SQLite-vec BLOB storage
   - Update `embedding_status = 'EMBEDDED'`

4. **Curation (NEW - Refactored Architecture)**
   - Uses `tools/scraper/curation.py` with `Top10Curator` class
   - **Smart Context Packing**:
     - Budget: 80% of `LLM_CONTEXT_CHAR_LIMIT` (default: 32,000 chars)
     - Sorting: Conclusion length DESC, then title ASC (tie-breaker)
     - Algorithm: Greedy packing of whole articles (NO slicing)
     - Dynamic target: `min(10, packed_count)`
   - **Validated Curation**:
     - 3-retry loop with LLM validation
     - Strict rules: Exactly target_count items, zero duplicate ULIDs, valid candidates only
     - Error context accumulation for retry prompts
     - Fallback: Sequential slice of packed candidates
   - **Integration**: `tools/scraper/tool.py` imports `Top10Curator` and executes `curate(slim_list, sync_llm_chat)`

5. **Persistence**
   - Raw JSON → `{ANYTHINGLLM_ARTIFACTS_DIR}/scraper/{batch_id}/scraper_output_{ts}.json`
   - Top 10 → AnythingLLM custom-documents via `write_artifact()` → `{ANYTHINGLLM_ARTIFACTS_DIR}/scraper/{batch_id}/top10.json`
   - Manifest → `{ANYTHINGLLM_ARTIFACTS_DIR}/scraper/{batch_id}/manifest.md` (text content)
   - Write `broadcast_batch` record (status: PENDING)
   - Returns structured payload with manifest in `summary` and input_args in `details`

**Key Fix Applied (2026-04-24):**
- **NameError Prevention**: `batch_id = ULID.generate()` moved before first artifact write
- **Input Echo**: All error payloads now include `details.input_args`
- **TypeError Prevention**: Curation handles null values via `str()` casting and `or []` fallbacks

### 5.2 Publisher Pipeline (Modular Architecture)

#### Phase 0: Validation
```python
validator = ArticleValidator(job_id)
valid_articles, skipped = validator.validate_batch(all_articles)
```
- Validates ULID and title presence
- Records skipped items in `job_items` (FAILED) if job_id present
- Returns filtered lists

#### Phase 1: Translation (Queue-with-Requeue)
```python
translator = BatchTranslator(job_id)
translated_map = await translator.translate_all(valid_articles)
```

**Key Features:**
- Load from job_items cache (cross-job aware, no job_id filter)
- Process batches of 10
- Requeue failed items up to `MAX_TRANSLATION_RETRIES=3`

**LLM Integration:**
- Uses `TRANSLATION_PROMPT` from `tools/publisher/prompt.py`
- Response format: `{"translations": [{"ulid": "...", "translated_title": "...", ...}]}`
- Parses via `parse_llm_json()` for robustness

#### Phase 2: Briefing Upload (Top-10)
```python
publisher = ChannelPublisher(client, state_mgr, job_id)
await publisher.publish_briefing(valid_articles, translated_map)
```

**Per Article:**
```python
# Skip if already completed
if self.state_mgr.state.is_completed("publish_briefing", ulid):
    continue

# Message assembly
body_text = f"*{title}*\n\n{summary}\n\n*Kesimpulan:* {conclusion}"
body_text = escape_markdown_v2(body_text)

# Send link (plain)
err1 = await client.send_message(briefing_chat, link, parse_mode=None)

# Send body (MarkdownV2 with smart_split_message)
for chunk in smart_split_message(body_text, 4000, ParseMode.MARKDOWN_V2):
    err2 = await client.send_message(briefing_chat, chunk, parse_mode=ParseMode.MARKDOWN_V2)

# Atomic update
if err1.success and err2.success:
    state_mgr.state.mark_completed("publish_briefing", ulid)
    state_mgr.persist_atomic(job_id, meta, "COMPLETED")
```

**Target:** `TELEGRAM_BRIEFING_CHAT_ID`  
**Idempotent:** Skips if `phase_state["publish_briefing"][ulid] == "COMPLETED"`  
**Link routing:** Sent with `parse_mode=None`

#### Phase 3: Archive Upload (Inventory)
```python
await publisher.publish_archive(valid_articles, translated_map)
```

**Per Article:**
```python
body_text = f"*{title}*\n\n*Kesimpulan:* {conclusion}\n\n*Ringkasan:*\n{summary}"
# (Same send + atomic update logic as Phase 2)
```

**Target:** `TELEGRAM_ARCHIVE_CHAT_ID`  
**Idempotent:** Skips if `phase_state["publish_archive"][ulid] == "COMPLETED"`  
**Link routing:** Sent with `parse_mode=None`

#### Phase 4: Finalization
```python
batch_status = calculate_status(...)
enqueue_write("UPDATE broadcast_batches SET status = ?, phase_state = ?", 
              (batch_status, json.dumps(state_mgr.state.to_dict())))
```

**Status Calculation:**
```python
if len(valid_articles) == 0:
    batch_status = "FAILED"
elif all_valid_translated and all_briefing_posted and all_archive_posted:
    batch_status = "COMPLETED"
else:
    batch_status = "PARTIAL"
```

### 5.3 Resume Capability

#### Scraper Resume:
```python
# In _run_botasaurus_scraper()
existing_validation = query job_items for step='validate' AND ulid AND status='COMPLETED'
existing_summary = query job_items for step='summary' AND ulid AND status='COMPLETED'

if existing_validation AND existing_summary:
    # Skip scraping
    # Regenerate embeddings only
    _emb = _sf.embed(article_text)
    # Write to DB
```

#### Publisher Resume:
```python
# Translation (Phase 1)
translated = get_all_translated_items(job_id)  # Cross-job aware
if ulid in [t['ulid'] for t in translated]:
    continue  # Skip LLM call

# Briefing (Phase 2)
if phase_state["publish_briefing"][ulid] == "COMPLETED":
    continue  # Skip Telegram send
```

#### Migration Resume:
- `run_migrations()` checks `current_v` vs `schema_version`
- If `current_v < BASE_SCHEMA_VERSION`, runs destructive reset (only if `SUMANAL_ALLOW_SCHEMA_RESET=1`)
- Individual `execute()` calls maintain atomicity
- Restore from backup on failure

### 5.4 Auto-Repair Logic

**In `database/writer.py`:**
```python
for attempt in range(MAX_REPAIR_RETRIES + 1):
    try:
        conn.execute(sql, params)
        break
    except Exception as e:
        if "no such table" in str(e):
            table_name = extract_table_name(e)
            if _attempt_table_repair(conn, table_name) and attempt < MAX_REPAIR_RETRIES:
                continue  # Retry once
        # Log & rollback
```

**Table Repair Scripts:** Stored in `database/schemas/` via `get_repair_script()`

### 5.5 Database Architecture Changes (New Lifecycle System)

#### **Fresh DB Fast-Path Initialization** (app.py lines 212-224):
```python
from database.lifecycle import run_database_lifecycle
await run_database_lifecycle()
```

**New `run_database_lifecycle()` Flow:**
1. Recover orphaned backups
2. Probe state: `exists, current_version = check_database_file_state()`
3. Fresh init if not exists
4. Destructive reset if corrupted (with flag)
5. Migrate if version outdated
6. Verify health and repair if needed

#### **Critical Fixes Applied:**

**Fix 1: Async Table Repair (database/lifecycle.py line 86)**
- `await _repair_missing_tables(missing)` instead of `create_task`
- Prevents startup completing before tables exist

**Fix 2: Corrupted File State (database/health.py lines 46-48, 57-60)**
- 0-byte files → `True, None` (not `False, None`)
- DatabaseError → `True, None` (not `False, None`)
- Forces lifecycle to handle corruption properly

**Fix 3: Version Alignment (database/migrations/__init__.py)**
- Uses authoritative `get_latest_version()` for target
- Adds final version stamp if base > migrations[-1]

### 5.6 Callback Mechanism (Robust Atomic Operations)

#### **System Architecture:**
```python
# bot/engine/worker.py - UnifiedWorkerManager

# 1. Tool execution completes
status_str = normal.get("status", "FAILED")  # "COMPLETED" or "PARTIAL"

# 2. Atomic callback with durable logging
if status_str in ("COMPLETED", "PARTIAL"):
    success = _do_callback_with_logging(job_id, normal.get("result"), attachments)
    if success:
        enqueue_write("UPDATE jobs SET status = 'COMPLETED' ...")
    else:
        enqueue_write("UPDATE jobs SET status = 'PENDING_CALLBACK', retry_count = 1 ...")
```

#### **_do_callback_with_logging() Function:**
- **Purpose**: Execute HTTP callback and log all operations atomically via `enqueue_write()`
- **Returns**: `True` on 2xx status, `False` otherwise

**Execution Flow:**
1. **Structured Callback Construction**
   - Parse tool output for `_callback_format: "structured"`
   - Extract: `tool_name`, `status`, `summary`, `details`, `artifacts`, `status_overrides`
   - Fallback: Raw string → summary truncate

2. **Markdown Generation**
   ```python
   from utils.callback_helper import format_callback_message, truncate_message
   
   callback_message = format_callback_message(
       job_id=job_id, status=status, tool_name=tool_name,
       summary=summary, details=details, artifacts=artifacts,
       artifacts_dir=artifacts_root, status_overrides=status_overrides
   )
   callback_message = truncate_message(callback_message, max_chars=12000)
   ```

3. **Payload Assembly (Golden Rule: NO Base64)**
   ```python
   callback_payload = {
       "message": f"TOOL_RESULT_CORRELATION_ID:{job_id}\n\n{callback_message}",
       "mode": "chat",
       "attachments": [],  # ENFORCED: No Base64 attachments.
       "reset": False,
   }
   ```

4. **Exponential Backoff Retry**
   ```python
   max_retries = 3
   base_delay = 2.0  # doubles each attempt (2s, 4s, 8s)
   
   while attempt < max_retries:
       try:
           resp = client.post(callback_url, json=callback_payload, headers=headers)
           resp.raise_for_status()
           enqueue_write("INSERT INTO job_logs ... Worker:Callback:Success")
           return True
       except httpx.HTTPStatusError as e:
           if 400 <= status_code < 500:
               enqueue_write("...Worker:Callback:ClientError...")  # No retry
               return False
           enqueue_write("...Worker:Callback:ServerError...")  # Retry
       except Exception as e:
           enqueue_write("...Worker:Callback:Transient...")  # Retry
       
       if attempt < max_retries:
           time.sleep(base_delay * (2 ** (attempt - 1)))
   
   enqueue_write("...Worker:Callback:MaxRetries...")
   return False
   ```

**Configuration:**
- `ANYTHINGLLM_CALLBACK_TIMEOUT`: HTTP timeout (default: 120s)
- `ANYTHINGLLM_CALLBACK_RETRY_DELAY_SECONDS`: Delay between retries (default: 30s)

#### **PENDING_CALLBACK State Behavior:**
- **Trigger**: Tool execution succeeded but callback HTTP failed
- **Storage**: `jobs` table with `status = 'PENDING_CALLBACK'`, `retry_count = N`, `result_json` preserved
- **Polling**: Database loop (1-second interval) checks `updated_at < now - {delay}`
- **Retry**: Spawns thread with `_retry_callback_only()` - no tool execution
- **Terminal**: After 3 failures, status → `PARTIAL`, logged to `job_logs`

#### **Golden Rules Enforced:**
1. ✅ No in-memory retry queues - Database is single source of truth
2. ✅ All writes via `enqueue_write()` - No concurrent write connections
3. ✅ No terminal state until HTTP 2xx - Jobs remain in `PENDING_CALLBACK`
4. ✅ Failed attachments logged as `WARNING` - Don't drop entire payload
5. ✅ Max 3 retries - Prevents infinite loops
6. ✅ **Custom-documents directive**: `attachments: []` always empty

### 5.7 Hybrid Search Implementation

#### **FTS5 Sanitization:**
```python
def sanitize_fts_query(query: str) -> str:
    sanitized = re.sub(r'[^\w\s]', ' ', query)
    return re.sub(r'\s+', ' ', sanitized).strip()
```
- Removes FTS5 reserved characters
- Prevents SQLite syntax errors

#### **Weighted RRF:**
```python
def weighted_rrf(vector_results, keyword_results, w_vec, w_kw, k=60):
    scores: Dict[str, float] = {}
    for rank, item in enumerate(vector_results, start=1):
        ulid = item['ulid']
        scores[ulid] = scores.get(ulid, 0.0) + (w_vec / (k + rank))
    # Similar for keyword results
    # Sort by fusion score descending
```
- Application-layer fusion (not SQL-level)
- Configurable weights via `config.BATCH_READER_VECTOR_WEIGHT` and `config.BATCH_READER_KEYWORD_WEIGHT`

#### **Execution Flow:**
1. Acquire batch_id and query
2. Extract valid ULIDs for batch
3. Parallel execute:
   - Vector search: `embedding MATCH ?` (if sqlite-vec available)
   - Keyword search: `scraped_articles_fts MATCH ?` (sanitized query)
4. Apply RRF with weights
5. Return fused results

### 5.8 Structured AI-Actionable Callbacks

#### **Problem Solved:**
- Previously, validation failures returned raw strings or exceptions → generic worker wrapper → non-actionable AI responses
- New system enforces **every exit path** emits structured payload with `status_overrides` explicitly telling the AI how to recover

#### **Implementation Pattern (All Tools):**
```python
def _fail(summary: str, next_steps: str) -> str:
    return json.dumps({
        "_callback_format": "structured",
        "tool_name": self.name,
        "status": "FAILED",
        "summary": summary,
        "details": {
            "input_args": args
        },
        "status_overrides": {
            "FAILED": {
                "description": "Tool encountered a validation error.",
                "next_steps": next_steps,
                "rerunnable": False
            }
        }
    }, ensure_ascii=False)

# Usage in early returns
if not batch_id or not query:
    return _fail("batch_id and query are required.", "Provide both 'batch_id' and 'query' parameters.")

conn = DatabaseManager.get_read_connection()
row = conn.execute("SELECT raw_json_path FROM broadcast_batches WHERE batch_id = ?", (batch_id,)).fetchone()
if not row or not row["raw_json_path"]:
    return _fail("Batch not found or missing raw data.", "Verify the batch_id is valid. If lost, use the `scraper` tool to generate a new batch.")

try:
    with open(row["raw_json_path"], "r", encoding="utf-8") as f:
        raw_data = json.load(f)
except Exception as e:
    return _fail(f"Failed to read batch data: {str(e)}", "Data may have been purged. Use the `scraper` tool to generate a new batch.")
```

**Benefits:**
- AI receives clear `FAILED` status with structured `next_steps`
- No confusion from raw error strings
- Tool-specific recovery instructions (e.g., "use scraper to generate new batch")
- Consistent format across all tools

---

## 6. Public Interfaces

### 6.1 POST /api/tools/{tool_name}

**Input:** (Validated by tool's `INPUT_MODEL`)
```json
{
  "args": {"target_site": "FT"},
  "client_metadata": {}
}
```

**Output:**
```json
{
  "job_id": "01J8XYZ...",
  "status": "QUEUED"
}
```

**Validation:**
- Uses tool's `INPUT_MODEL` if present
- SSRF/URL scanning via `scan_args_for_urls()` for security

### 6.2 GET /api/jobs/{job_id}

**Output:**
```json
{
  "job_id": "...",
  "status": "COMPLETED",
  "job_logs": [
    {"timestamp": "...", "level": "INFO", "tag": "...", "status_state": "RUNNING"}
  ]
}
```

### 6.3 DELETE /api/jobs/{job_id}

**Output:**
```json
{
  "status": "CANCELLING",
  "job_id": "..."
}
```

**Behavior:**
- Sets `jobs.status = 'CANCELLING'`
- Worker checks `cancellation_flag` in execution thread
- Tool must cooperatively check flag

### 6.4 GET /api/manifest

**Output:**
```json
{
  "tools": {
    "scraper": {
      "name": "scraper",
      "description": "...",
      "input_model": {...}
    }
  }
}
```

**Generated from:** `registry.py` introspection

### 6.5 GET /api/metrics

**Output:**
```json
{
  "total_jobs": 123,
  "active_jobs": 5,
  "pending_jobs": 10,
  "failed_jobs": 2,
  "average_processing_time_ms": 45000
}
```

**Source:** Direct DB queries

---

## 7. State, Persistence, and Data

### 7.1 Database Architecture

#### **Storage:**
- **Engine:** SQLite with WAL mode
- **File:** `data/anythingtools.db` (configurable)
- **Path:** Configured via `DATABASE_PATH` env var

#### **Connection Management:**
- **Writer Thread:** Single-writer via `database/writer.py`
- **Read Connections:** Thread-local in `database/connection.py`
- **Isolation:** `BEGIN EXCLUSIVE` for migrations, `enqueue_write()` for all mutations

#### **Data Lifecycle:**
```python
# Write path
enqueue_write("INSERT ...") → Background thread → DB connection

# Read path
conn = DatabaseManager.get_read_connection()
row = conn.execute("SELECT ...").fetchone()
```

### 7.2 Data Formats

#### **job_items.item_metadata (JSON):**
```json
{
  "step": "validate|translate|publish_briefing|publish_archive",
  "ulid": "01J8ABC...",
  "retry": 2,
  "timestamp": "2026-04-17T03:45:00.123Z",
  "model": "gpt-5.4-mini",
  "is_top10": true,
  "error": "Timeout after 3 attempts"
}
```

#### **broadcast_batches.phase_state (JSON):**
```json
{
  "validate": {"01J8ABC...": {"status": "COMPLETED"}},
  "translate": {"01J8ABC...": {"status": "COMPLETED"}},
  "publish_briefing": {"01J8ABC...": {"status": "COMPLETED"}},
  "publish_archive": {"01J8ABC...": {"status": "COMPLETED"}}
}
```

#### **Tool Output (Structured for Callback):**
```json
{
  "_callback_format": "structured",
  "tool_name": "scraper",
  "status": "COMPLETED",
  "summary": "### Scout Intelligence Briefing\n...",
  "details": {
    "input_args": {"target_site": "FT"},
    "batch_id": "01KPYP7T1112WJ5PZFBD98SB0X",
    "target_site": "FT",
    "total_articles": 85,
    "target_curated_count": 8,
    "actual_curated_count": 8,
    "inventory_count": 50
  },
  "artifacts": [{"filename": "top10.json", "type": "json", "description": "Curated Top 8 for FT."}],
  "status_overrides": {
    "COMPLETED": {
      "description": "Scrape successful.",
      "next_steps": "Query batch via batch_reader.",
      "rerunnable": false
    }
  }
}
```

### 7.3 Artifact Storage

#### **Path Structure:**
```
{ANYTHINGLLM_ARTIFACTS_DIR}/
├── scraper/
│   ├── {batch_id}/
│   │   ├── scraper_output_{ts}.json
│   │   ├── top10.json
│   │   └── manifest.md
├── draft_editor/
│   └── ...
```

#### **Atomic Writes:**
```python
# In artifact_manager.py
def write_artifact(tool_name, job_id, artifact_type, ext, content):
    safe_tool = re.sub(r"[^a-z_]", "", tool_name.lower())
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]", "", job_id)
    job_dir = target_dir / safe_tool / safe_job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    
    filename = f"{safe_type}.{safe_ext}"
    filepath = job_dir / filename
    temp_path = filepath.with_suffix(f".tmp{filepath.suffix}")
    
    with open(temp_path, mode, encoding=encoding) as fh:
        fh.write(content)
    temp_path.replace(filepath)  # Atomic rename
```

#### **Access:**
- Artifacts served via FastAPI static file mount at `/artifacts/`
- URLs constructed via `artifact_url_from_request()`

### 7.4 Migration Behavior

#### **Auto-Fold Mechanism:**
```python
# database/migrations/__init__.py
if len(active_migrations) > MAX_MIGRATION_SCRIPTS:
    # Fold oldest migration into BASE_SCHEMA_VERSION
    backup = extract_schema_from_memory_db()
    merge_into_domain_modules(backup)
    delete_migration_file(oldest)
```

#### **Backup/Restore:**
- Automatic backup before each migration
- Restored automatically on failure
- Located in same directory as DB file: `{db_path}.backup.{version}`

#### **Reset Behavior:**
- **Fresh DB:** No data loss, fast schema creation
- **Corrupted DB:** Destructive reset **only** if `SUMANAL_ALLOW_SCHEMA_RESET=1`
- **Version Mismatch:** Migrations run automatically

---

## 8. Dependencies & Integration

### 8.1 Runtime Dependencies

#### **Core Framework:**
- `fastapi` - API server
- `uvicorn` - ASGI server
- `httpx` - HTTP client for callbacks

#### **Browser Automation:**
- `botasaurus` - Headless browser scraping
- `selenium` - WebDriver backend

#### **Database:**
- `sqlite3` - Built-in, with WAL mode
- `sqlite-vec` - Vector search fallback (optional)

#### **AI/LLM:**
- `openai` - Azure OpenAI client
- `azure-identity` - Authentication

#### **Telegram:**
- `python-telegram-bot` v22.7 - Bot API
- `httpx` - Async HTTP client

#### **Data Processing:**
- `pandas` - Data manipulation (batch_reader)
- `pydantic` - Validation (API schemas)

#### **Utilities:**
- `pytz` - Timezone handling
- `python-dotenv` - Environment loading

### 8.2 Configuration Dependencies

#### **Required Environment Variables:**
```bash
# Database
DATABASE_PATH=./data/anythingtools.db

# AnythingLLM Integration
ANYTHINGLLM_ARTIFACTS_DIR=/path/to/anythingllm/custom-documents
ANYTHINGLLM_CALLBACK_URL=http://localhost:3000/api/tools/callback
ANYTHINGLLM_CALLBACK_TIMEOUT=120
ANYTHINGLLM_CALLBACK_RETRY_DELAY_SECONDS=30

# Telegram
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_BRIEFING_CHAT_ID=-1001234567890
TELEGRAM_ARCHIVE_CHAT_ID=-1001234567891

# Snowflake/Snowflake Embeddings
SNOWFLAKE_ACCOUNT=...
SNOWFLAKE_USER=...
SNOWFLAKE_PASSWORD=...
SNOWFLAKE_WAREHOUSE=...
SNOWFLAKE_DATABASE=...
SNOWFLAKE_SCHEMA=...

# Azure OpenAI
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini-2024-05-13

# Migration Behavior
SUMANAL_ALLOW_SCHEMA_RESET=0  # Set to 1 for dangerous reset
```

#### **Optional:**
```bash
# For testing without real dependencies
MOCK_TELEGRAM=1
MOCK_SNOWFLAKE=1
MOCK_OPENAI=1

# Performance tuning
WORKER_POLL_INTERVAL=1  # Seconds
MAX_CONCURRENT_JOBS=1  # Browser lock enforces serialization
```

### 8.3 Coupling Points

#### **High Coupling:**
1. **Database Schema → Migration Scripts**: Scripts depend on exact schema versions
2. **Tool → Worker**: Tools emit structured callbacks, worker parses them
3. **Artifact Manager → AnythingLLM Path**: Must have valid path at startup
4. **Telegram → Phase State**: Publisher state machine depends on exact phase names

#### **Medium Coupling:**
1. **Botasaurus → Browser Lock**: Single instance enforced by lock
2. **Snowflake → Embeddings**: Direct calls in scraper, fallback in vector_search
3. **Azure OpenAI → Response Format**: `_build_responses_payload()` maps to Azure API

#### **Low Coupling:**
1. **Logger**: Can be used independently
2. **Utils**: Text processing, metadata helpers are reusable

### 8.4 Environment Assumptions

#### **Platform:**
- **OS:** Linux, macOS, Windows (tested on all)
- **Python:** 3.10+
- **Network:** Outbound HTTPS required for Telegram, Azure, Snowflake
- **Disk:** Write access to `./data/` and `ANYTHINGLLM_ARTIFACTS_DIR`

#### **Runtime:**
- **Single Instance:** Cannot run multiple copies (database lock)
- **Background Writer:** Always active
- **Browser:** One instance at a time (single lock)

#### **External Services:**
- **AnythingLLM**: Must have custom-documents endpoint at configured path
- **Telegram**: Bot token and chat IDs must be valid
- **Snowflake**: Account credentials, warehouse, database must exist
- **Azure OpenAI**: Resource must be deployed with correct model

---

## 9. Setup, Build, and Execution

### 9.1 Prerequisites

#### **System Requirements:**
- Python 3.10 or higher
- 4GB RAM minimum (8GB recommended for browser operations)
- 2GB disk space for dependencies and data
- Outbound HTTPS access to:
  - Azure OpenAI endpoints
  - Telegram Bot API
  - Snowflake account

### 9.2 Installation

#### **Step 1: Clone and Prepare**
```bash
# Clone repository
git clone <repository>
cd anythingtools

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# OR
.venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Install sqlite-vec (optional, for vector search)
pip install sqlite-vec
```

#### **Step 2: Configuration**
```bash
# Create .env from template (if available)
cp .env.example .env

# Or manually create .env with required variables:
cat > .env << 'EOF'
DATABASE_PATH=./data/anythingtools.db
ANYTHINGLLM_ARTIFACTS_DIR=/path/to/anythingllm/custom-documents
ANYTHINGLLM_CALLBACK_URL=http://localhost:3000/api/tools/callback
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_BRIEFING_CHAT_ID=-1001234567890
TELEGRAM_ARCHIVE_CHAT_ID=-1001234567891
SNOWFLAKE_ACCOUNT=your_account
SNOWFLAKE_USER=your_user
SNOWFLAKE_PASSWORD=your_password
SNOWFLAKE_WAREHOUSE=your_warehouse
SNOWFLAKE_DATABASE=your_database
SNOWFLAKE_SCHEMA=your_schema
AZURE_OPENAI_API_KEY=your_api_key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini-2024-05-13
SUMANAL_ALLOW_SCHEMA_RESET=0
EOF

# Set permissions (Linux/macOS)
chmod 600 .env
```

#### **Step 3: Directory Setup**
```bash
# Create data directory
mkdir -p ./data

# Verify artifacts directory exists (must be created externally)
ls -la $ANYTHINGLLM_ARTIFACTS_DIR
# Should show: custom-documents/ subdirectory structure
```

### 9.3 First-Time Execution

#### **Startup Validation:**
```bash
# Start the service
python app.py

# Expected output:
# INFO: Starting AnythingTools...
# INFO: Validating ANYTHINGLLM_ARTIFACTS_DIR: /path/to/anythingllm/custom-documents
# INFO: Database file not found, creating fresh schema...
# INFO: Running database lifecycle...
# INFO: Fresh database initialized at version 6
# INFO: Writer thread started
# INFO: Worker poller started (1s interval)
# INFO: Uvicorn running on http://0.0.0.0:8000
```

#### **Shutdown:**
```
Ctrl+C
```

**Graceful shutdown:**
- Writer thread drains queue
- Browser lock released
- Database connections closed

### 9.4 Build Process

#### **No Build Required:**
- Pure Python application
- No compilation steps
- Dependencies installed via pip

#### **Docker (Optional):**
```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN mkdir -p /app/data

ENV DATABASE_PATH=/app/data/anythingtools.db
ENV ANYTHINGLLM_ARTIFACTS_DIR=/app/artifacts

CMD ["python", "app.py"]
```

### 9.5 Platform Constraints

#### **Operating System:**
- ✅ **Linux:** Fully supported (most tested)
- ✅ **macOS:** Fully supported
- ✅ **Windows:** Fully supported (paths use `pathlib`)

#### **Browser Automation:**
- **Chrome/Chromium:** Required for Botasaurus
- **Headless:** Default mode
- **Docker:** Requires `--cap-add=SYS_ADMIN` or similar for browser sandbox

#### **Database:**
- **SQLite Version:** 3.35.0+ required for WAL mode
- **File System:** Must support file locking
- **Network File Systems:** Not recommended (locking issues)

#### **Memory:**
- **Idle:** ~150MB
- **During Scrape:** ~800MB-1.2GB (browser + Python)
- **Peak (Publisher):** ~200MB (LLM batching)

---

## 10. Testing & Validation

### 10.1 What Testing Exists

#### **Browser Health Check (tests/test_browser_e2e.py):**
```python
def test_browser_health():
    """Launch browser, create driver, close"""
    from utils.browser_daemon import get_or_create_driver
    driver = get_or_create_driver()
    assert driver is not None
    driver.quit()
```

**Purpose:** Validates Botasaurus installation and browser availability

#### **Migration Test Outline (tests/test_migration_pipeline.py):**
```python
def test_migration_sequence():
    """Test migration order and fold mechanism"""
    # Outline only, requires manual DB setup
    pass
```

**Purpose:** Placeholder for migration validation

### 10.2 How to Run Tests

#### **Run All Tests:**
```bash
pytest tests/
```

#### **Run Specific Test:**
```bash
pytest tests/test_browser_e2e.py -v
```

#### **Run with Coverage:**
```bash
pip install pytest-cov
pytest --cov=tools --cov=bot --cov=database tests/
```

#### **Integration Testing (Manual):**
```bash
# 1. Start service
python app.py

# 2. Enqueue scraper job (in another terminal)
curl -X POST http://localhost:8000/api/tools/scraper \
  -H "Content-Type: application/json" \
  -d '{"args": {"target_site": "FT"}}'

# 3. Monitor job status
curl http://localhost:8000/api/jobs/{job_id}

# 4. Check artifacts
ls -la $ANYTHINGLLM_ARTIFACTS_DIR/scraper/{batch_id}/

# 5. Verify callback (if ANYTHINGLLM_CALLBACK_URL configured)
# Check logs for "Worker:Callback:Success"
```

### 10.3 Test Coverage Gaps

#### **Uncovered Areas:**
1. **Database Migrations**: No automated test for migration sequence
2. **Callback Retry Logic**: Mock HTTP server needed
3. **Telegram Integration**: Requires real bot token
4. **Snowflake Embeddings**: Requires real credentials
5. **Structured Callbacks**: No validation of output format
6. **Resume Logic**: Requires interrupted job state
7. **Auto-Repair**: No test for "no such table" scenario
8. **PENDING_CALLBACK Polling**: Time-based test needed
9. **Artifact Manager**: Path validation, atomic writes
10. **Curation Algorithm**: Edge cases (empty lists, null values)

#### **Recommended Additions:**
```python
# tests/test_curation.py
def test_curation_with_nulls():
    candidates = [
        {"ulid": "01A", "title": None, "conclusion": "text"},
        {"ulid": "01B", "title": "Title", "conclusion": None},
    ]
    curator = Top10Curator()
    result, count = curator.curate(candidates, mock_llm)
    assert len(result) == 2

# tests/test_callback_structure.py
def test_all_tools_emit_structured():
    """Verify _fail() and success payloads contain required keys"""
    required = ["_callback_format", "tool_name", "status", "summary", "details", "status_overrides"]
    # Check each tool
```

---

## 11. Known Limitations & Non-Goals

### 11.1 Hard Constraints

#### **Concurrency Restrictions:**
1. **Single Browser Instance**
   - **Why:** Botasaurus + Selenium limitations
   - **Impact:** Max 1 scrape job at a time
   - **Evidence:** `browser_lock.py`, `MAX_CONCURRENT_JOBS=1`

2. **Single-Writer Database**
   - **Why:** SQLite WAL mode limitations
   - **Impact:** All writes queued via `enqueue_write()`
   - **Evidence:** `writer.py`, `connection.py`

3. **No Dynamic Tool Loading**
   - **Why:** Security and determinism requirements
   - **Impact:** Only 4 tools (scraper, draft_editor, batch_reader, publisher)
   - **Evidence:** `registry.py` whitelist

#### **External Dependencies:**
1. **AnythingLLM Required for Artifacts**
   - **Why:** Custom-documents directory must exist
   - **Impact:** Service fails startup if `ANYTHINGLLM_ARTIFACTS_DIR` invalid
   - **Evidence:** `app.py` lifespan validation

2. **Azure OpenAI Required**
   - **Why:** No local LLM fallback
   - **Impact:** Scraper, publisher depend on API availability
   - **Evidence:** `clients/llm/` package

3. **Snowflake for Embeddings**
   - **Why:** Primary vector source
   - **Impact:** Scraping requires Snowflake credentials
   - **Evidence:** `utils/vector_search.py`

### 11.2 Implementation Limitations

#### **Scraper:**
1. **Botasaurus Only**
   - **Limitation:** Cannot use Playwright, Puppeteer, Scrapy
   - **Workaround:** None (architectural decision)
   - **Evidence:** `tools/scraper/task.py` imports Botasaurus

2. **JSON Schema Fallback Only**
   - **Limitation:** Only `BadRequestError` triggers fallback to `json_object`
   - **Impact:** Other errors (timeout, rate limit) fail immediately
   - **Evidence:** `tools/scraper/extraction.py` line ~200

3. **Top-10 Only**
   - **Limitation:** Cannot request Top-N where N != 10
   - **Change:** Recent update allows dynamic count, but still limited by packing
   - **Evidence:** `tools/scraper/curation.py` dynamic target

#### **Publisher:**
1. **Telegram-Only**
   - **Limitation:** No email, Slack, webhook support
   - **Evidence:** `utils/telegram/` package only

2. **Fixed Phases**
   - **Limitation:** validate → translate → publish_briefing → publish_archive (no customization)
   - **Evidence:** `utils/telegram/pipeline.py`

#### **Batch Reader:**
1. **Batch-ID Dependent**
   - **Limitation:** Cannot query without batch_id
   - **Evidence:** `tools/batch_reader/tool.py`

2. **Requires Manual Batch Creation**
   - **Limitation:** No automatic batch creation from raw artifacts
   - **Evidence:** Requires scraper output

### 11.3 Security Limitations

1. **No User Authentication**
   - **Impact:** Any HTTP request can enqueue jobs
   - **Mitigation:** Network isolation, firewall rules
   - **Evidence:** No auth middleware in `app.py`

2. **File System Access**
   - **Impact:** Can read/write any path configurable via env
   - **Mitigation:** Principle of least privilege
   - **Evidence:** `artifact_manager.py` has arbitrary path access

3. **Command Injection Risk**
   - **Impact:** Tools receive unvalidated strings
   - **Mitigation:** Input validation via Pydantic, SSRF scanning
   - **Evidence:** `api/routes.py` line 77 `scan_args_for_urls()`

### 11.4 Non-Goals (Will Not Be Implemented)

1. **Multi-User System**: Not planned
2. **GraphQL API**: REST only
3. **Real-time Streaming**: Polling model only
4. **GUI/Dashboard**: API-only
5. **Custom Tool Loading**: Whitelist model
6. **Distributed Execution**: Single instance
7. **Horizontal Scaling**: Not supported
8. **Fine-grained Permissions**: All-or-nothing
9. **Audit Logging (Beyond DB)**: DB logs are sufficient
10. **Export Formats**: JSON only

---

## 12. Change Sensitivity

### 12.1 Fragile Areas (High Risk)

#### **Database Schema:**
- **Location:** `database/schemas/`
- **Risk:** High
- **Why:** Every migration requires schema changes
- **Evidence:** `database/migrations/` contains version-specific scripts
- **Impact of Change:**
  - Requires new migration script
  - Must update all `json_extract()` queries
  - Must update `make_metadata()` helper
  - May trigger auto-fold mechanism

#### **Structured Callback Format:**
- **Location:** `utils/callback_helper.py`, all 4 tools
- **Risk:** High
- **Why:** Worker depends on exact fields
- **Evidence:** `bot/engine/worker.py` line 586 `if normal.get("_callback_format") == "structured"`
- **Impact of Change:**
  - Worker parsing logic breaks
  - AI receives non-actionable responses
  - Callbacks fail silently

#### **Artifact Manager Path Logic:**
- **Location:** `utils/artifact_manager.py`
- **Risk:** Medium-High
- **Why:** Affects all 4 tools' output locations
- **Evidence:** `write_artifact()` used by all tools
- **Impact of Change:**
  - Breaks existing artifact references
  - Requires path migration
  - AnythingLLM integration breaks

#### **Worker Polling Logic:**
- **Location:** `bot/engine/worker.py` `UnifiedWorkerManager.run()`
- **Risk:** Medium
- **Why:** Affects job lifecycle, resume behavior
- **Evidence:** 1-second polling interval, status transitions
- **Impact of Change:**
  - May lose job states
  - Resume logic may break
  - Retry loops may become infinite

### 12.2 Stable Areas (Low Risk)

#### **Tool Implementations:**
- **Location:** `tools/*/tool.py`
- **Risk:** Low
- **Why:** Isolated logic, standard pattern
- **Evidence:** Each tool follows `BaseTool` pattern
- **Safe Changes:**
  - Prompt updates
  - Algorithm improvements (like curation.py)
  - Error messages

#### **Logger:**
- **Location:** `utils/logger/`
- **Risk:** Low
- **Why:** Independent component
- **Evidence:** Used via `get_dual_logger()`
- **Safe Changes:** Formats, levels, outputs

#### **Telegram Client:**
- **Location:** `utils/telegram/`
- **Risk:** Low-Medium
- **Why:** Modular, isolated
- **Evidence:** Only publisher uses it
- **Safe Changes:** Message formatting (must maintain escape fixes)

#### **Migration Archive:**
- **Location:** `database/migrations_archive/`
- **Risk:** None
- **Why:** History only, not executed
- **Evidence:** Read-only for reference
- **Safe Changes:** None needed

### 12.3 Easy Extension Points

#### **Adding New Target Sites:**
```python
# tools/scraper/targets.py
VALID_TARGET_NAMES = [
    "FT", "Bloomberg", "Reuters",  # Existing
    "NewYorkTimes",                # Add here
    "TheVerge",                    # Add here
]
```
- **Files Changed:** 1
- **Testing:** Manual scrape test
- **Risk:** Near zero (no schema changes)

#### **Updating Prompts:**
```python
# tools/publisher/prompt.py
TRANSLATION_PROMPT = """
Updated instructions here...
"""
```
- **Files Changed:** 1
- **Testing:** Manual publish test
- **Risk:** Low (pure logic change)

#### **Adjusting Curation Algorithm:**
```python
# tools/scraper/curation.py
def _pack_context(self, candidates):
    # Change sorting key or budget calculation
    budget = int(getattr(config, "LLM_CONTEXT_CHAR_LIMIT", 40000) * 0.9)  # Change 0.8 to 0.9
```
- **Files Changed:** 1
- **Testing:** Verify artifact sizes
- **Risk:** Low (isolated to scraper)

### 12.4 Widespread Refactoring Required

#### **Tool Addition:**
1. Create `tools/newtool/` with `tool.py`, `Skill.py`, `INPUT_MODEL`
2. Add to `core_tools` in `registry.py`
3. Update `bot/engine/worker.py` (if custom handling needed)
4. Add to `README`
5. Add `_fail()` helpers
6. **Files:** 5-6
7. **Risk:** Medium (integration testing required)

#### **Migration Schema Change:**
1. Create `database/migrations/v009_*.py`
2. Update `database/schemas/` domain modules
3. Update all `json_extract()` queries (grep for pattern)
4. Update `utils/metadata_helpers.py`
5. Test auto-fold behavior
6. **Files:** 8-10
7. **Risk:** High (data loss potential)

#### **Publisher Phase Addition:**
1. Update `utils/telegram/publisher.py` pipeline
2. Update `broadcast_batches.phase_state` schema
3. Update `database/schemas/jobs.py`
4. Create v009 migration
5. Update status calculation
6. **Files:** 5-6
7. **Risk:** Medium-High (state machine complexity)

#### **Callback System Changes:**
1. Update `utils/callback_helper.py` status definitions
2. Update all 4 tools' `_fail()` helpers
3. Update `bot/engine/worker.py` parsing logic
4. Update `utils/artifact_manager.py` URLs
5. Test all failure modes
6. **Files:** 10+
7. **Risk:** Very High (breaks all tools)