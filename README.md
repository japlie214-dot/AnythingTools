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

### What It Does NOT Do
- Does not execute autonomous agent loops or reasoning chains
- Does not dynamically discover or load tools beyond the hardcoded whitelist
- Does not support concurrent browser operations (single browser lock)
- Does not provide real-time streaming responses
- Does not offer multi-tenancy or user isolation
- Does not implement dynamic tool loading or runtime tool discovery

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
- **`app.py`** - FastAPI entrypoint with lifespan lifecycle
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
- **`tool.py`** - Scout Mode, Botasaurus integration, Intelligent Manifest generation
- **`task.py`** - Botasaurus scraper implementation
- **`prompt.py`** - Scraping prompts
- **`scraper_prompts.py`** - Contains `SUMMARIZATION_SCHEMA` with anyOf syntax for nullable `error` field
- **`summary_prompts.py`** - Summarization prompts
- **`targets.py`** - Valid target site configuration
- **`extraction.py`** - JSON schema attempt with fallback to `json_object`, hardened exception handling
- **`persistence.py`** - Structured JSON parsing with null handling
- **Resume behavior**: Skips if both validation and summary exist in job_items

**Draft Editor (`tools/draft_editor/`):**
- **`tool.py`** - Atomic SWAP operations, PENDING status lock

**Batch Reader (`tools/batch_reader/`):**
- **`tool.py`** - Semantic search using hybrid RRF, filtered by batch_id
- **New Hybrid Search**: Uses `utils/hybrid_search.py` for vector + keyword fusion

**Publisher (`tools/publisher/`):**
- **`tool.py`** - Orchestrates `utils.telegram.pipeline.PublisherPipeline`
- **`Skill.py`** - Skill wrapper
- **`prompt.py`** - Contains `TRANSLATION_PROMPT` with strict MarkdownV2 rules (raw string literal)

### 3.4 Execution Layer (`bot/`)

#### Engine (`bot/engine/`):
- **`worker.py`** - `UnifiedWorkerManager` with 1-second polling loop
  - Polls jobs prioritizing `INTERRUPTED`
  - Spawns execution threads
  - Crash recovery (3 strikes → `ABANDONED`)
  - AnythingLLM callback on `COMPLETED`/`PARTIAL`
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

#### Telegram Package (New Modular Architecture):
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
- **`browser_lock.py`** - `threading.Lock` for browser exclusivity
- **`browser_daemon.py`** - Driver lifecycle management
- **`browser_utils.py`** - Safe navigation utilities
- **`som_utils.py`** - State-of-mind synchronization
- **`metadata_helpers.py`** - JSON metadata construction/parsing
- **`vector_search.py`** - Direct Snowflake client calls, SQLite-vec fallback
- **`hybrid_search.py`** - NEW: FTS5 sanitization, Weighted RRF, orchestration for hybrid search

#### Logging:
- **`logger/`** - Dual logging (console + file) with structured payloads

### 3.7 Clients (`clients/`)
- **`snowflake_client.py`** - Direct Snowflake connection
- **`llm/`** - Azure OpenAI wrapper with **critical updates:**
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
**Table:** `job_items` (after v004 migration)
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

4. **Curation**
   - LLM prompt: "Return ONLY a JSON object with key 'top_10'"
   - Top 10 selected from slim_list

5. **Persistence**
   - Raw JSON → `artifacts/scrapes/scraper_output_{ts}.json`
   - Top 10 → `artifacts/scrapes/top_10_{batch_id}.json`
   - Write `broadcast_batch` record (status: PENDING)
   - Intelligent Manifest stored in `broadcast_batches`

### 5.2 Publisher Pipeline (New Modular Architecture)

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
1. **Attachment Processing**
   - Skip missing files and log `WARNING` to `job_logs`
   - Base64-encode valid files
   - Never fails entire callback on individual file errors

2. **HTTP Request**
   ```python
   with httpx.Client(timeout=config.ANYTHINGLLM_CALLBACK_TIMEOUT) as client:
       resp = client.post(url, json=callback_payload, headers=headers)
       resp.raise_for_status()
   ```

3. **Durable Logging** (All via `enqueue_write()`)
   - Success: `Worker:Callback:Success` with attachment count
   - HTTP Error: `Worker:Callback:Error` with status_code in payload_json
   - File Error: `Worker:Callback:FileError` or `Worker:Callback:FileMissing`

**Configuration:**
- `ANYTHINGLLM_CALLBACK_TIMEOUT`: HTTP timeout in seconds (default: 120)
- `ANYTHINGLLM_CALLBACK_RETRY_DELAY_SECONDS`: Delay between retry attempts (default: 30)

#### **Database-Driven Retry Loop:**

**Polling Query (worker.py _run_loop):**
```python
delay = config.ANYTHINGLLM_CALLBACK_RETRY_DELAY_SECONDS
rows = conn.execute(
    f"SELECT job_id, session_id, tool_name, args_json, status, result_json, retry_count FROM jobs "
    f"WHERE status IN ('QUEUED', 'INTERRUPTED') "
    f"   OR (status = 'PENDING_CALLBACK' AND updated_at < datetime('now', '-{delay} seconds')) "
    f"ORDER BY status ASC, created_at ASC LIMIT 5"
).fetchall()
```

**Retry Handler (`_retry_callback_only()`):**
```python
# In separate thread, no tool re-execution
attachments = result_data.get("attachment_paths", [])
tool_output = result_data.get("result", result_data)
success = _do_callback_with_logging(job_id, tool_output, attachments)

if success:
    enqueue_write("UPDATE jobs SET status = 'COMPLETED' ...")
else:
    new_retry_count = retry_count + 1
    if new_retry_count >= 3:
        enqueue_write("UPDATE jobs SET status = 'PARTIAL' ...")
        enqueue_write("INSERT INTO job_logs ... 'Max callback retries exceeded'")
    else:
        enqueue_write("UPDATE jobs SET retry_count = ?, updated_at = ? ...")
```

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

### 5.7 Hybrid Search Implementation (New)

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
- Configurable weights via `config.BATCH_READER_VECTOR_WEIGHT` and `BATCH_READER_KEYWORD_WEIGHT`

#### **Execution Flow:**
1. Acquire batch_id and query
2. Extract valid ULIDs for batch
3. Parallel execute:
   - Vector search: `embedding MATCH ?` (if sqlite-vec available)
   - Keyword search: `scraped_articles_fts MATCH ?` (sanitized query)
4. Apply RRF with weights
5. Return fused results

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
- SSRF/URL scanning via `scan_args_for_urls()`

### 6.2 GET /api/jobs/{job_id}

**Output:**
```json
{
  "job_id": "...",
  "status": "COMPLETED",
  "job_logs": [
    {"timestamp": "...", "level": "INFO", "tag": "...", "status_state": "RUNNING"}
  ],
  "final_payload": {
    "batch_id": "...",
    "artifacts": ["artifacts/scrapes/top_10_....json"],
    "artifact_urls": ["http://host/artifacts/scrapes/top_10_....json"]
  }
}
```

### 6.3 DELETE /api/jobs/{job_id}

**Behavior:**
- Marks job as `CANCELLING` in DB
- Sets cancellation flag in `WorkerManager` if job is running
- Returns `202 Accepted`

### 6.4 GET /api/manifest

**Output:** MCP-style schemas for 4 tools with `INPUT_MODEL`

### 6.5 GET /api/metrics

**Output:**
```json
{
  "write_queue_size": 0,
  "active_jobs": 0,
  "registered_tools": 4
}
```

---

## 7. State, Persistence, and Data

### 7.1 Database Architecture

**File:** `data/sumanal.db` (WAL mode enabled)

**Core Tables (Post v006 Migration):**
- `jobs` - Job lifecycle
- `job_items` - Granular step tracking (JSON metadata)
- `job_logs` - Structured logs
- `broadcast_batches` - Publisher batches (JSON phase_state)
- `scraped_articles` - Raw content
- `scraped_articles_vec` - Vector embeddings
- `scraped_articles_fts` - FTS5 keyword index (virtual table)
- `pdf_parsed_pages` - PDF text
- `pdf_parsed_pages_vec` - PDF vectors
- `token_usage` - LLM cost tracking

### 7.2 Data Lifecycle

**Scraper Data:**
- Raw articles: Stored in SQLite (scraped_articles)
- Embeddings: Stored in SQLite (scraped_articles_vec) or BLOB fallback
- Top 10 JSON: Written to `artifacts/scrapes/top_10_{batch_id}.json`
- Batch metadata: `broadcast_batches` table

**Publisher Data:**
- Phase state: JSON in `broadcast_batches.phase_state`
- Translation cache: `job_items` with `step='translate'`
- Delivery tracking: `job_items` with `step='publish_*'`
- **Incremental updates**: After each article via `enqueue_write()`

**Migration Data:**
- Backup: `*.db.bak` before migration
- Archive: Folded migrations in `database/migrations_archive/`
- Version: Stored in `PRAGMA user_version`

### 7.3 Cleanup Jobs

**Startup (Lines 276-307 in app.py):**
- **Recovery scan**: Requeues `RUNNING` and `INTERRUPTED` to `QUEUED`
- **Stale cleanup**: Jobs >7 days old → `FAILED`, delete their `job_items`

**Shutdown:**
- Purge `pdf_parsed_pages`

---

## 8. Dependencies & Integration

### 8.1 External Libraries (Evidence from `requirements.txt`)

**Core Framework:**
- `fastapi`, `uvicorn`, `pydantic` - API
- `httpx` - HTTP client

**Browser:**
- `botasaurus` - Browser automation
- `playwright` (implicit - installed separately)

**Scraping:**
- `ddgs` - Search engine
- `beautifulsoup4` - HTML parsing

**PDF:**
- `reportlab`, `pypdf`, `pdfplumber`, `pymupdf`, `paddleocr`, `paddlepaddle`

**Data:**
- `pandas` - Analysis
- `yfinance`, `edgartools`, `sec-edgar-downloader` - Finance

**Database:**
- `sqlite-vec` - Vector extension (optional, graceful fallback)

**Cloud:**
- `snowflake-connector-python` - Embeddings
- `openai` - LLM

**Utilities:**
- `python-dotenv`, `colorama`, `psutil`

### 8.2 Integration Points

**AnythingLLM Callback (Evidence in `bot/engine/worker.py`):**
- Triggers on `COMPLETED` or `PARTIAL`
- POST to `{ANYTHINGLLM_BASE_URL}/api/v1/workspace/{SLUG}/chat`
- Payload includes job_id correlation and attachments

**Snowflake Client (Evidence in `clients/snowflake_client.py`):**
- Direct authentication with private key
- `async_embed()` and `embed()` methods
- Used in scraper and publisher

**Telegram API (Evidence in new modular files):**
- `https://api.telegram.org/bot{TOKEN}/sendMessage`
- Uses `parse_mode="MarkdownV2"` (or None for links/fallback)
- Rate-limited with 3.1s delay via `GlobalRateLimiter`

---

## 9. Setup, Build, and Execution

### 9.1 Prerequisites

- Python 3.11+
- Playwright Chromium: `playwright install chromium`
- Optional: `sqlite-vec` extension binary

### 9.2 Installation Steps

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Install browser
playwright install chromium

# 3. Configure environment
cp .env.example .env
# Edit .env with credentials (TELEGRAM_BOT_TOKEN, TELEGRAM_*_CHAT_ID, etc.)

# 4. Start application
uvicorn app:app --reload --port 8000
```

**New `.env` requirements:**
```bash
TELEGRAM_BOT_TOKEN=your_token_here
TELEGRAM_BRIEFING_CHAT_ID=your_chat_id
TELEGRAM_ARCHIVE_CHAT_ID=your_chat_id
SUMANAL_ALLOW_SCHEMA_RESET=0  # Set 1 for development
BATCH_READER_VECTOR_WEIGHT=0.6
BATCH_READER_KEYWORD_WEIGHT=0.4

# Callback Configuration
ANYTHINGLLM_BASE_URL=http://localhost:3001
ANYTHINGLLM_API_KEY=your_api_key_here
ANYTHINGLLM_WORKSPACE_SLUG=my-workspace
ANYTHINGLLM_CALLBACK_TIMEOUT=120
ANYTHINGLLM_CALLBACK_RETRY_DELAY_SECONDS=30
```

### 9.3 Database First Run

**What happens:**
1. `app.py` lifespan calls `await run_database_lifecycle()`
2. Detects fresh state, calls `initialize()`
3. Writer thread writes schema via `enqueue_execscript()`
4. Version stamped via `enqueue_write("PRAGMA user_version = 6")`
5. 10s timeout prevents hangs

**Expected logs:**
```
DB:Health - All expected tables verified.
DB:Lifecycle - No database found, running fresh init.
DB:WriterStart - Database writer started.
DB:Lifecycle - Initializing fresh database to v6
DB:Schema - Fresh database; schema created and stamped to v6 via writer queue.
API:Worker:Start - Unified WorkerManager started
```

### 9.4 Migration Execution

**Automatic on startup for existing DBs:**
```bash
# Logs show:
DB:Lifecycle - Recover from v004, migrating...
DB:Migration - Applying migration v4: Convert job_items.step_identifier to item_metadata JSON
DB:Migration - Applying migration v5: Add PARTIAL status to jobs table
DB:Migration - Applying migration v6: Deprecate posted_*_ulids and add phase_state to broadcast_batches
DB:Migration - Applying migration v7: Create FTS5 table and vec_rowid index
DB:Migration - All migrations applied. Schema version: 6
```

**Auto-fold triggered if migrations > 3:**
```
DB:Migration:Autofold - Auto-fold: folding oldest migration v004_...
DB:Migration:Autofold - Wrote updated schema module
DB:Migration:Autofold - Moved v004_... -> database/migrations_archive/
```

### 9.5 Testing Basic Flow

```bash
# 1. Enqueue scraper
curl -X POST -H "X-API-Key: dev_default_key_change_me_in_production" \
  -H "Content-Type: application/json" \
  -d '{"args": {"target_site": "FT"}}' \
  http://localhost:8000/api/tools/scraper

# 2. Check job status (returns job_id in step 1)
curl -H "X-API-Key: dev_default_key_change_me_in_production" \
  http://localhost:8000/api/jobs/{job_id}

# 3. Monitor logs
tail -f logs/application.log
```

**Expected artifacts:**
- `artifacts/scrapes/scraper_output_{ts}.json`
- `artifacts/scrapes/top_10_{batch_id}.json`
- `broadcast_batches` entry with batch_id

---

## 10. Testing & Validation

### 10.1 E2E Tests

**`tests/test_browser_e2e.py`:**
- Launches Chrome
- Navigates to Google
- Verifies health

**`tests/test_migration_pipeline.py` (Outline):**
1. Discovery and validation
2. Transaction rollback simulation
3. Auto-fold with 4 migrations
4. Version alignment verification
5. Vector index preservation

### 10.2 Manual Validation

**Schema Version:**
```bash
sqlite3 data/sumanal.db "PRAGMA user_version;"
# Should return 6 (after v006 migration)
```

**Migration Status:**
```bash
ls database/migrations/v*.py
# Should show v005, v006, v007, v008
# v004 is in archive (folded), v008 is callback retry
```

**Table Structure (Post v008):**
```bash
# 1. Broadcast batches phase_state (unchanged from v006)
sqlite3 data/sumanal.db "PRAGMA table_info(broadcast_batches);"
# Should show phase_state TEXT column
# Should NOT show posted_research_ulids or posted_summary_ulids

# 2. Jobs table includes PENDING_CALLBACK status
sqlite3 data/sumanal.db "PRAGMA table_info(jobs);"
# Should show: status column with CHECK constraint including 'PENDING_CALLBACK'
# Should show: retry_count INTEGER NOT NULL DEFAULT 0

# 3. Jobs status from current data
sqlite3 data/sumanal.db "SELECT DISTINCT status FROM jobs;"
# Should include: PENDING_CALLBACK (if any callback retries pending)
```

**Job Items Metadata:**
```bash
sqlite3 data/sumanal.db "SELECT json_extract(item_metadata, '$.step') as step FROM job_items LIMIT 1;"
# Should return 'translate' or 'publish_briefing' etc.
```

**Writer Queue Health:**
```
# In logs: DB:WriterStart, DB:WriterStop should appear
# On fresh install: "Fresh database; schema created and stamped to v6 via writer queue"
```

**Callback System Validation:**
```bash
# Check config values exist
grep -E "ANYTHINGLLM_CALLBACK_TIMEOUT|ANYTHINGLLM_CALLBACK_RETRY_DELAY_SECONDS" config.py
# Should show both with int(os.getenv(...)) patterns

# View callback logs in database
sqlite3 data/sumanal.db "SELECT timestamp, tag, level, message FROM job_logs WHERE tag LIKE '%Callback%' ORDER BY timestamp DESC LIMIT 5;"
# Should show: Worker:Callback:Success, Worker:Callback:Error, etc.
```

**Migration v008 Verification:**
```bash
cat database/migrations/v008_pending_callback.py
# Should show: version = 8
# Should show: PENDING_CALLBACK added to CHECK constraint
# Should show: CREATE TABLE jobs_new with status CHECK including PENDING_CALLBACK
```

---

## 11. Known Limitations & Non-Goals

### 11.1 Explicit Limitations

| Limitation | Reason | Workaround |
|-----------|--------|------------|
| **Single-writer DB** | SQLite WAL limitation | N/A (by design) |
| **4 tools only** | Security lockdown | Manual whitelist edit |
| **No concurrent jobs** | File-level browser lock | Job-level parallelism |
| **Manual schema reset** | Data safety | `SUMANAL_ALLOW_SCHEMA_RESET=1` |
| **Bounded auto-repair** | Infinite loop prevention | Manual intervention |
| **Migration limit (3)** | Version discipline | Auto-fold mechanism |
| **Callback silence** | Fail-fast design | Monitor logs |
| **Database-driven retry only** | No in-memory queues | Polling loop checks PENDING_CALLBACK |
| **Max 3 callback retries** | Prevents cascades | Job → PARTIAL after 3 failures |
| **Callback timeout (120s)** | Prevents hangs | Configurable via ANYTHINGLLM_CALLBACK_TIMEOUT |
| **Retry delay (30s)** | Rate limiting | Configurable via ANYTHINGLLM_CALLBACK_RETRY_DELAY_SECONDS |
| **No sqlite-vec fallback** | Extension dependency | BLOB storage |
| **Fresh DB fast-path only** | Optimization strategy | N/A (performance gain) |
| **MarkdownV2 strict validation** | Prevent API 400 errors | Plain-text fallback |

### 11.2 Non-Goals (Wontfix)

- **Autonomous agent loops** → Deterministic only
- **Dynamic tool discovery** → Whitelist lockdown
- **Real-time streaming** → Batch design
- **Multi-tenant isolation** → Single-session focus
- **Automatic schema upgrades** → Requires explicit consent
- **Infinite retry** → Bounded prevents cascades

### 11.3 Design Rationale

**Why "Deterministic"?**
- Predictable execution path (no LLM loops)
- Clear state transitions
- Resume without side effects
- Explicit failure modes

**Why "Single-writer"?**
- Prevents database corruption
- Simplifies concurrency model
- Enables WAL checkpointing
- Forces clear write boundaries

**Why "Migration System"?**
- **Domain segregation**: Monolithic → maintainable modules
- **Autonomous management**: Zero manual intervention
- **Transaction safety**: BEGIN EXCLUSIVE + rollback guards
- **Version discipline**: 3-file limit forces cleanup
- **Environment agnostic**: Works across dev/staging/production

**Why "Fresh DB Fast-Path"?**
- **Performance**: Eliminates migration overhead for new installs
- **Clean state**: No legacy baggage
- **Atomicity**: Writer queue ensures single-writer compliance
- **Observed need**: Migrations add ~2-5s on fresh DBs

---

## 12. Change Sensitivity (Fragile Areas)

### 12.1 Critical Components (High Coupling)

**Database Migrations:**
- **Fragility**: Any change to `item_metadata` structure breaks queries
- **Evidence**: `database/reader.py`, `database/job_queue.py` all use `json_extract()`
- **Impact**: Requires new migration to rebuild indexes
- **Easiest extension**: Add new step types (update `make_metadata()` only)

**Telegram Publisher Pipeline:**
- **Fragility**: `escape_markdown_v2()` regex patterns must match Telegram spec
- **Evidence**: `utils/telegram/publisher.py` message assembly
- **Impact**: 400 errors from malformed MarkdownV2
- **Easiest extension**: Add new target chat (beyond briefing/archive)
- **Critical fix**: Character range and double-backslash bugs must remain fixed

**Whitelist Registry:**
- **Fragility**: `tools/registry.py` hardcodes 4 tool names
- **Evidence**: Line 48 `core_tools = ["scraper", "draft_editor", "publisher", "batch_reader"]`
- **Impact**: New tools require registry modification
- **Easiest extension**: Add to whitelist, ensure `INPUT_MODEL` defined

**Fresh DB Fast-Path:**
- **Fragility**: Version check and writer queue coordination
- **Evidence**: `app.py` lines 222-233 using `enqueue_execscript()` + `enqueue_write()`
- **Impact**: Incorrect version stamping causes migration chaos
- **Safety**: 10s timeout prevents infinite waits

**Callback Retry System:**
- **Fragility**: `PENDING_CALLBACK` state logic and retry behavior
- **Evidence**: `bot/engine/worker.py` lines 138-147 (SQL query), 213-241 (`_retry_callback_only`)
- **Impact**: Wrong delay values cause storm of retries; no retry_count increment → infinite loop
- **Easiest extension**: Add new callback endpoint (requires updating `_do_callback_with_logging` URL)
- **Critical invariants**:
  - Must use `enqueue_write()` only (no direct writes)
  - Must preserve `result_json` in PENDING_CALLBACK state
  - Must check `updated_at < datetime('now', '-{delay} seconds')` for retry eligibility
  - Cannot exceed max 3 retries before PARTIAL

### 12.2 Changes Requiring Widespread Refactoring

**Adding New Tool Type:**
1. Create `tools/newtool/` with `tool.py`, `Skill.py`, `INPUT_MODEL`
2. Add to `core_tools` whitelist in `registry.py`
3. Update `bot/engine/worker.py` job execution (if custom handling needed)
4. Add test in `tests/`
5. Update README

**Migration Schema Change:**
1. Create migration script in `database/migrations/`
2. Update `database/schemas/` domain modules
3. Update all `json_extract()` queries
4. Update `make_metadata()` and `parse_metadata()` in `utils/metadata_helpers.py`
5. Test auto-fold behavior

**Publisher Phase Addition:**
1. Update `utils/telegram/publisher.py` pipeline sequence
2. Update `broadcast_batches.phase_state` schema
3. Update `database/schemas/jobs.py`
4. Create migration for phase_state structure
5. Update status calculation logic

**Environment Variable Migration:**
1. Move hardcoded defaults to `.env`
2. Update `config.py` to use `os.getenv()` without defaults
3. Document required variables
4. Test configuration loading

### 12.3 Safe Extension Points

**Adding New Target Sites:**
- **Location**: `tools/scraper/targets.py`
- **Impact**: Minimal, just add to `VALID_TARGET_NAMES`
- **No schema changes**: Works immediately

**Adding New LLM Prompts:**
- **Location**: `tools/publisher/prompt.py`, `tools/scraper/prompts.py`
- **Impact**: Pure logic change, no schema
- **Safety**: Can be tested independently

**Updating Telegram Formats:**
- **Location**: `utils/telegram/publisher.py` lines in publish_briefing/publish_archive
- **Impact**: Only affects Telegram output
- **Safety**: Localized change, easy to revert

**Adjusting MarkdownV2 Escaping:**
- **Location**: `utils/text_processing.py` `escape_markdown_v2()`
- **Impact**: All Telegram messages
- **Safety**: Must maintain both bug fixes:
  - Character range: `[\\_*\[\]()~`>#+=|{}.!\-]` (hyphen at end)
  - Replacement: `r'\\\1'` (single backslash)
- **Test**: Send messages with special chars to verify

**Adding New Callback Configuration:**
- **Location**: `config.py` (add `ANYTHINGLLM_CALLBACK_TIMEOUT`, `ANYTHINGLLM_CALLBACK_RETRY_DELAY_SECONDS`)
- **Impact**: Affects all callback operations
- **Safety**: Must update README and .env.example