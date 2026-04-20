# AnythingTools - Deterministic Tool Hosting Service

## Executive Summary

**AnythingTools** is a deterministic tool-hosting service that executes four whitelisted tools via HTTP API. The system has evolved from autonomous agent architecture into a direct execution engine with robust state management, automatic database recovery, and resume-capable pipelines.

**Current Schema Version:** 5 (via migration system)  
**Architecture:** Single-writer SQLite with WAL mode, autonomous migration management  
**Tool Count:** 4 (scraper, draft_editor, batch_reader, publisher)  
**Resume Capability:** Full granular tracking via job_items table  
**Auto-Repair:** Schema-aware automatic recovery for 17 core tables  
**Migration System:** Domain-driven schema, auto-folding to 3-file limit, transaction safety with rollback guards  
**Publisher Status:** Queue-with-requeue architecture, PARTIAL status support, item-centric tracking with phase_state JSON

---

## 1. High-Level Architecture

### 1.1 System Evolution (Evidence-Based Analysis)

The system transitioned from autonomous agent loops to deterministic execution:

**Legacy (Deprecated - Evidence in `deprecated/` directory):**
- UnifiedAgent with reasoning loops
- Dynamic tool discovery
- Uncontrolled LLM interaction
- Finance, Research, Polymarket, Quiz tools

**Current (Active - Observable from codebase):**
- Direct tool execution via worker poller (`bot/engine/worker.py`)
- Hardcoded tool whitelist in `tools/registry.py` (line 48)
- Controlled LLM usage (Publisher translation only)
- State machine with resume capability
- **Migration System** - Introduced in `database/migrations/` (evident from v004_step_to_metadata.py, v005_jobs_partial.py, v006_publisher_phase_state.py)
- **Publisher Queue Architecture** - Complete rewrite in `utils/telegram_publisher.py` with 3-phase pipeline
- **Phase State** JSON column replaces legacy `posted_*_ulids` arrays

### 1.2 High-Level Data Flow

```
API Request → Job Queue (QUEUED) → Worker Poller → Tool Execution → AnythingLLM Callback → COMPLETED
```

**Key Characteristics:**
- **Event-driven polling:** 1-second interval in `bot/engine/worker.py`
- **No autonomous loops:** Direct execution only
- **Single-writer database:** Prevents concurrent write conflicts (`database/connection.py`)
- **Background writer thread:** Async DB operations with batching (`database/writer.py`)
- **Lifecycle hooks:** Startup recovery, zombie cleanup, reconciliation (lines 276-307 in `app.py`)
- **Autonomous migrations:** Auto-folding with transaction safety (`database/migrations/__init__.py`)

### 1.3 Architecture Components Map

```
┌─────────────────────────────────────────────────────────────┐
│                       Entry Point (app.py)                   │
│  • FastAPI lifespan hooks                                   │
│  • Static file mounting (artifacts/)                        │
│  • Schema initialization & migration                        │
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
                                  └───────┬──────┴──────┬───────┘
                                          │             │
                                     ┌────▼──────┐   ┌───▼────┐
                                     │  SQLite   │   │ Tools  │
                                     │  WAL DB   │   │ Scraper│
                                     │           │   │ etc.   │
                                     └───────────┘   └────────┘
```

---

## 2. Repository Structure

### 2.1 Root Directory

- **`app.py`** - FastAPI entrypoint with lifespan lifecycle (Vec0 validation, Chrome cleanup, DB init, recovery scan)
- **`config.py`** - Configuration with Telegram, Azure, Snowflake credentials
- **`requirements.txt`** - All dependencies including Botasaurus, Snowflake, PaddleOCR

### 2.2 Database Layer (`database/`)

#### Core Modules:
- **`connection.py`** - `DatabaseManager` with thread-local connections, WAL mode, sqlite_vec detection
- **`writer.py`** - Background writer thread with queue, auto-repair logic, 1-retry limit for missing tables
- **`schema.py`** - Proxy layer delegating to `database/schemas/` and `database/migrations/`
- **`job_queue.py`** - Job operations with JSON metadata in v005+
- **`reader.py`** - Read operations with JSON extraction queries
- **`blackboard.py`** - State tracking using JSON metadata

#### Migration System (`database/migrations/`):
- **`__init__.py`** - Autonomous runner with auto-fold logic, transaction safety, backup/restore
- **`v004_step_to_metadata.py`** - Converts `step_identifier` → `item_metadata` JSON (v3 → v4)
- **`v005_jobs_partial.py`** - Adds `PARTIAL` status to jobs (v4 → v5)
- **`v006_publisher_phase_state.py`** - Migrates `posted_*_ulids` → `phase_state` JSON (v5 → v6)

#### Schema Registry (`database/schemas/`):
- **`__init__.py`** - Domain registry pattern, `BASE_SCHEMA_VERSION = 3`, `MAX_MIGRATION_SCRIPTS = 3`
- **`jobs.py`** - Jobs, job_items, job_logs, broadcast_batches
- **`finance.py`** - Financial tables (unused in current pipeline)
- **`vector.py`** - Vector tables with sqlite-vec fallback
- **`pdf.py`** - PDF parsing tables
- **`token.py`** - Token usage tracking

#### Migration Archive (`database/migrations_archive/`):
- Stores folded migrations
- `README.md` explains purpose

### 2.3 Tools (`tools/`)

#### Registry & Base:
- **`registry.py`** - Whitelist enforcement (4 tools only), dynamic loading, manifest generation
- **`base.py`** - `BaseTool` abstract class

#### Active Tools:

**Scraper (`tools/scraper/`):**
- **`tool.py`** - Scout Mode, Botasaurus integration, Intelligent Manifest generation
- **`task.py`** - Botasaurus scraper implementation
- **`prompt.py`** - Scraping prompts
- **`scraper_prompts.py`** - **Changed `Conclusion:` → `Kesimpulan:`** (observable)
- **`summary_prompts.py`** - Summarization prompts
- **`targets.py`** - Valid target site configuration

**Draft Editor (`tools/draft_editor/`):**
- **`tool.py`** - Atomic SWAP operations, PENDING status lock

**Batch Reader (`tools/batch_reader/`):**
- **`tool.py`** - Semantic search filtered by batch_id

**Publisher (`tools/publisher/`):**
- **`tool.py`** - Translation and Telegram delivery orchestrator
- **`Skill.py`** - Skill wrapper
- **`prompt.py`** - **NEW: Contains `TRANSLATION_PROMPT` with MarkdownV2 rules and Kesimpulan requirement** (observable)

### 2.4 Bot/Execution Layer (`bot/`)

#### Engine (`bot/engine/`):
- **`worker.py`** - `UnifiedWorkerManager` with 1-second polling loop
  - Polls jobs prioritizing `INTERRUPTED`
  - Spawns execution threads
  - Crash recovery (3 strikes → `ABANDONED`)
  - AnythingLLM callback on `COMPLETED`/`PARTIAL`
- **`tool_runner.py`** - `run_tool_safely` wrapper with timeout

### 2.5 API Layer (`api/`)

- **`routes.py`** - Endpoints:
  - `POST /api/tools/{tool_name}` - Enqueue job (202)
  - `GET /api/jobs/{job_id}` - Status + logs
  - `DELETE /api/jobs/{job_id}` - Cancellation request
  - `GET /api/manifest` - Tool schemas
  - `GET /api/metrics` - System metrics
- **`schemas.py`** - Pydantic models for input validation

### 2.6 Utilities (`utils/`)

#### Core Utilities:
- **`telegram_publisher.py`** - **COMPLETE REWRITE**:
  - 3-phase pipeline (Translation, Briefing, Archive)
  - **`TelegramErrorInfo`** dataclass
  - **`escape_markdown_v2()`** integration (from `text_processing.py`)
  - **Kesimpulan localization** in assembly (lines 343-344, 398-399)
  - Cross-job translation loading
  - Extreme rate-limit abort logic
- **`text_processing.py`** - **NEW: `escape_markdown_v2()`** with selective entity preservation
- **`browser_lock.py`** - `threading.Lock` for browser exclusivity
- **`browser_daemon.py`** - Driver lifecycle management
- **`browser_utils.py`** - Safe navigation utilities
- **`som_utils.py`** - State-of-mind synchronization
- **`metadata_helpers.py`** - JSON metadata construction/parsing
- **`vector_search.py`** - Direct Snowflake client calls, SQLite-vec fallback

#### Logging:
- **`logger/`** - Dual logging (console + file) with structured payloads

### 2.7 Clients (`clients/`)

- **`snowflake_client.py`** - Direct Snowflake connection
- **`llm/`** - Azure OpenAI wrapper

### 2.8 Deprecated (`deprecated/`)

- **Legacy architecture evidence** - UnifiedAgent, dynamic tools, unused tool types

### 2.9 Tests (`tests/`)

- **`test_browser_e2e.py`** - Browser health check
- **`test_migration_pipeline.py`** - Migration test outline

---

## 3. Core Concepts & Domain Model

### 3.1 Job Lifecycle State Machine

```
QUEUED → RUNNING → COMPLETED/FAILED
         ↓
   INTERRUPTED (recovery on startup)
         ↓
   PAUSED_FOR_HITL (manual intervention)
         ↓
   ABANDONED (after 3 crashes)
```

**Global Statuses:** `QUEUED`, `RUNNING`, `COMPLETED`, `PARTIAL`, `FAILED`, `CANCELLING`, `INTERRUPTED`, `PAUSED_FOR_HITL`, `ABANDONED`

### 3.2 Job Items (Granular Tracking)

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

**Query Pattern:**
```sql
SELECT json_extract(item_metadata, '$.step') as step,
       json_extract(item_metadata, '$.ulid') as ulid,
       status, output_data
FROM job_items
WHERE job_id = ? AND status = 'COMPLETED'
```

### 3.3 Broadcast Batches (Publisher State)

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

**Batch Status Logic (Publisher):**
- `COMPLETED`: 100% of valid items translated AND all briefings and archives posted
- `PARTIAL`: Mixed outcomes (some success, some failures)
- `FAILED`: All invalid or complete failure

### 3.4 Migration Version Chain

**BASE_SCHEMA_VERSION = 3** (in `database/schemas/__init__.py`)

**Active Migrations:**
1. **v004** - `step_identifier` → `item_metadata` JSON
2. **v005** - Adds `PARTIAL` status to jobs, updates job_items metadata persistence
3. **v006** - Migrates `posted_research_ulids`, `posted_summary_ulids` → `phase_state` JSON

**Auto-Fold Mechanism:**
- If active migrations > 3 (`MAX_MIGRATION_SCRIPTS`), oldest folds into BASE_SCHEMA_VERSION
- All tables re-extracted from memory DB, merged into domain modules
- Migration file deleted

---

## 4. Detailed Behavior & Key Workflows

### 4.1 Scraper Execution Flow

1. **Initialization**
   - Validate target site against `VALID_TARGET_NAMES`
   - Acquire `browser_lock`
   - Get driver from `browser_daemon`

2. **Scraping (via Botasaurus)**
   - Run `_run_botasaurus_scraper()` in thread
   - Per-article: validate → summarize → embed
   - Resume check: Skip if `validation_passed` and `summary_generated` exist in `job_items`

3. **Embedding Generation**
   - Direct Snowflake calls: `snowflake_client.embed(text)`
   - Fallback: SQLite-vec BLOB storage
   - Update `embedding_status = 'EMBEDDED'`

4. **Curation**
   - LLM prompt: "Return ONLY a JSON object with key 'top_10'"
   - Top 10 selected from slim_list

5. **Persistance**
   - Raw JSON → `artifacts/scrapes/scraper_output_{ts}.json`
   - Top 10 → `artifacts/scrapes/top_10_{batch_id}.json`
   - Write broadcast_batch record (status: PENDING)

6. **Manifest Generation**
   - Intelligent Manifest stored in `broadcast_batches`
   - Format includes Top 10 + Extended Inventory

### 4.2 Publisher Pipeline (Complete Rewrite - 3 Phases)

#### Phase 0: Validation
- Validates all articles for ULID and title
- Records skipped items in `job_items` (FAILED)
- Populates `valid_articles` and `skipped_articles`

#### Phase 1: Translation (Queue-with-Requeue)
```python
# Producer: Queue-based translation with retry
queue: deque = deque()
# Load from job_items cache (cross-job aware)
# Process batches of 10
# Requeue failed items up to MAX_TRANSLATION_RETRIES=3
```

**Key Changes:**
- **Cross-job resumption**: `job_id` filter removed from `_load_cached_translations()`
- **LLM prompt**: Now includes MarkdownV2 rules and Kesimpulan requirement

#### Phase 2: Briefing Upload (Top-10)
- **Target**: `TELEGRAM_BRIEFING_CHAT_ID`
- **Messages per article**: 2 (link + body)
- **Body format**: `*{title}*\n\n{summary}\n\n*Kesimpulan:* {conclusion}`
- **Idempotent**: Skips if `phase_state["publish_briefing"][ulid] == "COMPLETED"`

#### Phase 3: Archive Upload (Inventory)
- **Target**: `TELEGRAM_ARCHIVE_CHAT_ID`
- **Messages per article**: 2 (link + body)
- **Body format**: `*{title}*\n\n*Kesimpulan:* {conclusion}\n\n*Ringkasan:*\n{summary}`
- **Idempotent**: Skips if `phase_state["publish_archive"][ulid] == "COMPLETED"`

#### Phase 4: Finalization
- Calculates accurate `batch_status`
- Updates `broadcast_batches` with `phase_state` JSON
- Logs metrics

**Rate Limit Safety:**
- 3.1s enforced delay between messages
- `_send_msg()` returns `TelegramErrorInfo`
- Extreme rate limits (>config) → `raise Exception()` → Batch aborts → Status becomes `PARTIAL`

### 4.3 Resume Capability

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
translated = get_all_translated_items(job_id)  # Cross-job aware now
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

### 4.4 Auto-Repair Logic

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

---

## 5. Configuration & Environment

### 5.1 Critical Variables in `config.py`

```python
# Telegram Publisher
TELEGRAM_BOT_TOKEN: str
TELEGRAM_BRIEFING_CHAT_ID: str = "-1001832461600"
TELEGRAM_ARCHIVE_CHAT_ID: str = "-1002574049512"
TELEGRAM_MESSAGE_DELAY: float = 3.1  # Enforced
TELEGRAM_MAX_MESSAGE_LENGTH: int = 4000  # Global limit
TELEGRAM_MAX_RETRY_AFTER: int = 120  # Extreme threshold

# Azure OpenAI
AZURE_KEY: str
AZURE_ENDPOINT: str
AZURE_DEPLOYMENT: str = "gpt-5.4-mini"

# Snowflake
SNOWFLAKE_ACCOUNT: str
SNOWFLAKE_USER: str
SNOWFLAKE_PRIVATE_KEY_PATH: str = "snowflake_private_key.p8"

# Schema Management
SUMANAL_ALLOW_SCHEMA_RESET: str = "0"  # Destructive migration flag

# Paths
ARTIFACTS_ROOT: str = "artifacts"
```

### 5.2 Optional Variables

```python
# Logging
TELEMETRY_DRY_RUN: bool = False

# Job Watchdog
JOB_WATCH_INTERVAL_SECONDS: int = 300
JOB_STALE_THRESHOLD_SECONDS: int = 28800  # 8 hours

# Chutes (Alternative LLM provider)
CHUTES_API_TOKEN: str
CHUTES_MODEL: str = "meta-llama/Llama-3.3-70B-Instruct"
```

---

## 6. API Interfaces

### 6.1 POST /api/tools/{tool_name}

**Input:** (Validated by tool's `INPUT_MODEL`)
```json
{
  "args": "{\"target_site\": \"FT\"}",  // Generic dict
  "client_metadata": {}  // Optional
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

**Telegram API (Evidence in `utils/telegram_publisher.py`):**
- `https://api.telegram.org/bot{TOKEN}/sendMessage`
- Uses `parse_mode="MarkdownV2"`
- Rate-limited with 3.1s delay

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
# Edit .env with credentials

# 4. Start application
uvicorn app:app --reload --port 8000
```

### 9.3 Database First Run

**What happens:**
1. `app.py` lifespan calls `init_db()`
2. `init_db()` creates v3 schema from `database/schemas/`
3. Migration runner activates
4. Writer thread starts
5. `broadcast_batches` table created

**Expected logs:**
```
DB:WriterStart - Database writer started
DB:Schema - Schema initialization and migrations completed via init_db()
API:Worker:Start - Unified WorkerManager started
```

### 9.4 Migration Execution

**Automatic on startup:**
```bash
# Logs show:
DB:Migration - Applying migration v4: Convert job_items.step_identifier to item_metadata JSON
DB:Migration - Applying migration v5: Add PARTIAL status to jobs table
DB:Migration - Applying migration v6: Deprecate posted_*_ulids and add phase_state to broadcast_batches
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
# Should show v005 and v006 only (v004 folded)
```

**Table Structure (Post v006):**
```bash
sqlite3 data/sumanal.db "PRAGMA table_info(broadcast_batches);"
# Should show phase_state TEXT column
# Should NOT show posted_research_ulids or posted_summary_ulids
```

**Job Items Metadata:**
```bash
sqlite3 data/sumanal.db "SELECT json_extract(item_metadata, '$.step') as step FROM job_items LIMIT 1;"
# Should return 'translate' or 'publish_briefing' etc.
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
| **No sqlite-vec fallback** | Extension dependency | BLOB storage |

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
- **Evidence**: `utils/telegram_publisher.py` lines 343-344, 398-399
- **Impact**: 400 errors from malformed MarkdownV2
- **Easiest extension**: Add new target chat (beyond briefing/archive)

**Whitelist Registry:**
- **Fragility**: `tools/registry.py` hardcodes 4 tool names
- **Evidence**: Line 48 `core_tools = ["scraper", "draft_editor", "publisher", "batch_reader"]`
- **Impact**: New tools require registry modification
- **Easiest extension**: Add to whitelist, ensure `INPUT_MODEL` defined

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
1. Update `utils/telegram_publisher.py` pipeline sequence
2. Update `broadcast_batches.phase_state` schema
3. Update `database/schemas/jobs.py`
4. Create migration for phase_state structure
5. Update status calculation logic

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
- **Location**: `utils/telegram_publisher.py` lines 343-344, 398-399
- **Impact**: Only affects Telegram output
- **Safety**: Localized change, easy to revert