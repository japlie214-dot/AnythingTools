# AnythingTools - Deterministic Tool Hosting Service

## Executive Summary

**AnythingTools** is a deterministic tool-hosting service that executes four whitelisted tools via HTTP API. The system has evolved from autonomous agent architecture into a direct execution engine with robust state management, automatic database recovery, and resume-capable pipelines.

**Current Schema Version:** 3 via migration system (target: 4)  
**Architecture:** Single-writer SQLite with WAL mode, autonomous migration management  
**Tool Count:** 4 (scraper, draft_editor, batch_reader, publisher)  
**Resume Capability:** Full granular tracking via job_items table  
**Auto-Repair:** Schema-aware automatic recovery for 17 core tables  
**Migration System:** Domain-driven schema, auto-folding to 3-file limit, transaction safety with rollback guards

---

## 1. Architecture Overview

### 1.1 System Evolution

The system transitioned from autonomous agent loops to deterministic execution:

**Legacy (Deprecated):**
- UnifiedAgent with reasoning loops
- Dynamic tool discovery
- Uncontrolled LLM interaction
- Finance, Research, Polymarket, Quiz tools

**Current (Active):**
- Direct tool execution via worker poller
- Hardcoded tool whitelist (4 tools only)
- Controlled LLM usage (Publisher translation only)
- State machine with resume capability
- **Database Migration System** (NEW) - Autonomous migration management

### 1.2 High-Level Data Flow

```
API Request → Job Queue (QUEUED) → Worker Poller → Tool Execution → AnythingLLM Callback → COMPLETED
```

**Key Characteristics:**
- **Event-driven polling:** 1-second interval
- **No autonomous loops:** Direct execution only
- **Single-writer database:** Prevents concurrent write conflicts
- **Background writer thread:** Async DB operations with batching
- **Lifecycle hooks:** Startup recovery, zombie cleanup, reconciliation
- **Autonomous migrations:** Auto-folding with transaction safety

---

## 2. Core Components

### 2.1 API Layer (`api/`)

#### `api/routes.py`
**Endpoints:**
- `POST /api/tools/{tool_name}` - Enqueue job (202 Accepted)
- `GET /api/jobs/{job_id}` - Job status with logs
- `DELETE /api/jobs/{job_id}` - Request cancellation
- `GET /api/manifest` - Tool schemas
- `GET /api/metrics` - System metrics

**Request Format:**
```json
{
  "args": { /* tool-specific */ },
  "client_metadata": { /* optional */ }
}
```

**Response Format:**
```json
{
  "job_id": "01J8XYZ...",
  "status": "QUEUED"
}
```

**Status States:** `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLING`, `INTERRUPTED`, `PAUSED_FOR_HITL`, `ABANDONED`

#### `api/schemas.py`
Pydantic models for input validation of all four tools.

### 2.2 Execution Engine (`bot/engine/`)

#### `bot/engine/worker.py` - UnifiedWorkerManager

**Core Loop:**
```python
def _run_loop(self):
    while not self._stop_event.is_set():
        # 1. Poll jobs (prioritize INTERRUPTED)
        # 2. Mark as RUNNING
        # 3. Spawn execution thread
        # 4. Sleep 1s
```

**Job Execution:**
```python
def _run_job(self, job_id, session_id, tool_name, args, cancellation_flag):
    # 1. Create tool instance
    # 2. Run tool_safely with cancellation flag
    # 3. Parse result (JSON or string)
    # 4. Update job status
    # 5. Invoke AnythingLLM callback (if completed)
    # 6. Handle crashes (3 strikes → ABANDONED)
```

**Startup Recovery (lines 284-296 in `app.py`):**
- Scans for `RUNNING` and `INTERRUPTED` jobs
- Requeues them to `QUEUED` status
- Enables automatic resume on restart

**Crash Recovery Logic:**
- 1st crash → Log, sleep 10s, set `INTERRUPTED`
- 2nd crash → Log, sleep 10s, set `INTERRUPTED`
- 3rd crash → Set `ABANDONED`, purge from retry

#### `bot/engine/tool_runner.py` - run_tool_safely

Safety wrapper with error handling and timeout management.

### 2.3 Tool Registry (`tools/`)

#### `tools/registry.py` - ToolRegistry

**Whitelist Implementation (line 48):**
```python
core_tools = ["scraper", "draft_editor", "publisher", "batch_reader"]
```

**Registration Process:**
1. Iterates only whitelisted directories
2. Import `tool.py` and `Skill.py` modules
3. Extracts `INPUT_MODEL` for schema validation
4. Registers `BaseTool` subclasses
5. Validates tool names against Azure OpenAI constraints

**Manifest Generation:**
- Returns MCP-style schemas for external integration
- Includes input validation models

### 2.4 Database Layer

#### `database/connection.py` - DatabaseManager
- Thread-local connections
- WAL mode enabled
- Automatic `sqlite_vec` detection
- Connection pooling

#### `database/writer.py` - Background Writer

**Key Features:**
- Single writer thread with queue
- Batch commit optimization
- **Auto-repair logic** for missing tables
- `MAX_REPAIR_RETRIES = 1` (prevents infinite loops)

**Auto-Repair Flow:**
1. Catch `no such table` error
2. Extract table name via regex
3. Fetch DDL from `TABLE_REPAIR_SCRIPTS`
4. Execute repair
5. Retry original operation once

#### `database/schema.py` - Schema Management (Proxy Layer)

**Updated Architecture:** Migration system replaces monolithic schema

**Proxy Functions:**
```python
def get_schema_version() -> int:
    """Returns current schema version (discovers from migrations)"""
    
def get_init_script() -> str:
    """Proxy to database.schemas.get_init_script()"""
    
def get_repair_script(table_name: str) -> Optional[str]:
    """Proxy to database.schemas.get_repair_script()"""
    
def init_db() -> None:
    """Main entry point: runs migrations, handles recovery"""
```

**Critical Change:** No longer contains embedded migration logic. Delegates to `database/migrations/`.

#### `database/migrations/` - NEW Migration System

**Purpose:** Autonomous migration management with safety guarantees

**Components:**

**`__init__.py` - Migration Runner:**
- `perform_auto_fold()` - Reduces migrations to ≤ 3 files
- `run_migrations(conn)` - Safe execution with backup/restore
- `_discover_migrations()` - Sequential version validation
- `get_latest_version()` - Migration version discovery

**Safety Mechanisms:**
- **WAL Checkpoint:** `PRAGMA wal_checkpoint(TRUNCATE)` before migration
- **DB Backup:** File-level backup using `shutil.copy2()`
- **Exclusive Transaction:** `BEGIN EXCLUSIVE` locks all connections
- **FK Disable:** `PRAGMA foreign_keys=OFF` during migration
- **Per-Statement Execute:** Individual `conn.execute()` (NOT `executescript()`)
- **FK Validation:** `PRAGMA foreign_key_check` after migration
- **Safe Rollback Guard:** Try/except around `ROLLBACK`

**`v004_step_to_metadata.py` - Initial Migration:**
- **Version:** 4
- **Purpose:** Convert `step_identifier` → `item_metadata`
- **Design:** Individual `execute()` calls maintain transaction atomicity
- **SQL:**
  ```sql
  CREATE TABLE job_items_new (...)
  INSERT INTO job_items_new ...
  DROP TABLE job_items
  ALTER TABLE job_items_new RENAME TO job_items
  CREATE INDEX ...
  ```

#### `database/schemas/` - Domain Schema Registry (NEW)

**Purpose:** Atomized schema modules replacing monolithic definition

**Structure:**
```
database/schemas/
├── __init__.py          # Registry: BASE_SCHEMA_VERSION=3, MAX_MIGRATION_SCRIPTS=3
├── jobs.py              # Job/queue tables (Version 3 state)
├── finance.py           # Financial tables (Not used in current pipeline)
├── vector.py            # Vector embedding tables (+sqlite-vec fallback)
├── pdf.py               # PDF parsing tables
└── token.py             # Token usage tables
```

**Registry Pattern:**
```python
# database/schemas/__init__.py
BASE_SCHEMA_VERSION = 3
MAX_MIGRATION_SCRIPTS = 3

def get_init_script() -> str:
    """Composes canonical schema from all domain modules"""
    
def get_repair_script(table_name: str) -> Optional[str]:
    """Returns specific table DDL"""
```

**Domain Module Examples:**

**`jobs.py` (Version 3 - Current State):**
```python
TABLES = {
    "job_queue": """CREATE TABLE job_queue (
        id TEXT PRIMARY KEY,
        tool_name TEXT,
        args TEXT,
        status TEXT,
        created_at TEXT
    )""",
    "job_items": """CREATE TABLE job_items (
        id INTEGER PRIMARY KEY,
        job_id INTEGER NOT NULL REFERENCES job_queue(id),
        step_identifier TEXT NOT NULL,  # ← Version 3 column
        created_at TEXT NOT NULL
    )"""
}
```

**`vector.py` (SQLite-vec compatibility):**
```python
TABLES = {
    "article_embeddings": """
        -- sqlite-vec virtual table (if available)
        CREATE VIRTUAL TABLE article_embeddings USING vec0(
            embedding float[768]
        );
        -- Fallback: CREATE TABLE ... (BLOB storage)
    """
}
```

#### `database/migrations_archive/` - Historical Storage
- Stores folded migrations
- `README.md` documents purpose

#### `database/job_queue.py` - Job Operations

**Signature Changes:**
```python
# OLD
def add_job_item(job_id: str, step_identifier: str, input_data: str) -> None: ...

# NEW (after v004 migration)
def add_job_item(job_id: str, item_metadata: str, input_data: str) -> None: ...
```

**Updated Functions:**
- `create_job()` - Creates job with tool name and args
- `add_job_item()` - Persists JSON metadata
- `update_item_status()` - Updates status with JSON
- `get_interrupted_job()` - Resume discovery

#### `database/reader.py` - Read Operations

**New Functions:**
```python
def get_top10_items(job_id: str) -> List[Dict[str, Any]]:
    # SELECT json_extract(item_metadata, '$.ulid') as ulid
    # FROM job_items 
    # WHERE job_id=? AND json_extract(item_metadata, '$.is_top10')=true
    
def get_all_translated_items(job_id: str) -> List[Dict[str, Any]]:
    # SELECT json_extract(item_metadata, '$.ulid') as ulid, output_data
    # FROM job_items
    # WHERE job_id=? AND json_extract(item_metadata, '$.step')='translate'
```

**Updated Functions:**
- `get_job_with_steps()` - Parses JSON for legacy compatibility
- Returns structured steps with metadata

#### `database/blackboard.py` - State Tracking

**Method Updates:**
```python
# OLD
claim_step(job_id: str, step_identifier: str): 
    "INSERT... WHERE step_identifier=?"

# NEW (uses JSON extraction)
claim_step(job_id: str, step_identifier: str):
    metadata = make_metadata(step=..., ulid=...)
    "INSERT... WHERE json_extract(item_metadata, '$.step')=?" 
```

**BlackboardService Methods:**
- `initialize_checklist()` - Creates step entries
- `claim_step()` - Atomic step claim
- `complete_step()` - Mark complete with output
- `fail_step()` - Record error
- `get_state()` - Returns parsed state

---

## 3. Utilities and Core Modules

### 3.1 Configuration (`config.py`)

**Critical Parameters:**
```python
TELEGRAM_MESSAGE_DELAY: float = 3.1  # Enforced rate limiting
TELEGRAM_BRIEFING_CHAT_ID: str | None  # Top-10 delivery
TELEGRAM_ARCHIVE_CHAT_ID: str | None   # Full inventory
ANYTHINGLLM_BASE_URL: str              # Callback destination
CHROME_USER_DATA_DIR: str              # Browser profile
```

### 3.2 Metadata Helpers (`utils/metadata_helpers.py`)

**Centralized JSON Structure:**
```python
def make_metadata(
    step_type: str,      # "translate", "publish_briefing", "publish_archive"
    ulid: str,
    retry: int = 0,
    model: Optional[str] = None,
    error: Optional[str] = None,
    is_top10: bool = False,
    **extra: Any
) -> str  # Returns JSON string
```

**Parsing Functions:**
```python
def parse_metadata(metadata_json: str) -> Dict[str, Any]:
    # Validates JSON and applies defaults
    # Prevents destructive dictionary recreation
    
def increment_retry(metadata_json: str) -> str:
    # Thread-safe retry increment
    
def add_error(metadata_json: str, error_msg: str) -> str:
    # Attaches error without losing context
```

**Usage Pattern:**
```python
# Creating metadata
metadata = make_metadata(STEP_TRANSLATE, article_ulid, is_top10=True)

# Querying with JSON extraction
sql = "SELECT * FROM job_items WHERE json_extract(item_metadata, '$.step') = ?"
cursor.execute(sql, (STEP_TRANSLATE,))
```

### 3.3 Telegram Publisher (`utils/telegram_publisher.py`)

**Complete Pipeline Rewrite (3-Phase):**

**Phase 1: Translation (Producer)**
```python
async def _phase1_translate_all(self):
    # 1. Query job_items for completed translations
    # 2. Skip cached (resume capability)
    # 3. Call LLM for new translations
    # 4. Persist to job_items with status=COMPLETED
```

**Phase 2: Briefing Upload (Consumer)**
```python
async def _phase2_upload_briefing(self):
    # 1. Build Top-10 list
    # 2. Query job_items for pub_a_{ulid}
    # 3. Skip sent messages
    # 4. Send via _send_msg() with rate limit
    # 5. Persist delivery status
```

**Phase 3: Archive Upload (Consumer)**
```python
async def _phase3_upload_archive(self):
    # 1. Build full inventory
    # 2. Query job_items for pub_b_{ulid}
    # 3. Batch messages with smart splitting
    # 4. Rate-limited delivery
    # 5. Persist status
```

**Critical Safety Features:**
- **Boolean return from `_send_msg()`**: Detects failures
- **Silent failure becomes `PARTIAL` status**: Never data loss
- **3.1s enforced delay**: Prevents Telegram rate limits
- **Job items deduplication**: Idempotent operations

### 3.4 Browser Management

**`utils/browser_lock.py`:**
- `threading.Lock` (NOT asyncio.Lock)
- Prevents concurrent browser access
- Safe release pattern

**`utils/browser_daemon.py`:**
- Driver lifecycle management
- Lazy initialization
- Zombie cleanup on startup

**`utils/som_utils.py`:**
- Single-tab enforcement
- State-of-mind synchronization

### 3.5 Vector Search (`utils/vector_search.py`)

**Embedding Generation:**
```python
# Direct Snowflake client (no wrapper)
_emb = snowflake_client.embed(text)
_eb = struct.pack(f"{len(_emb)}f", *_emb)
```

**Pattern:**
- **Resume path**: Direct `snowflake_client.embed()` calls
- **No fallback wrappers**: Removed `generate_embedding_sync()`
- **SQLite-vec integration**: Fallback to BLOB storage

---

## 4. Tool Specifications

### 4.1 Scraper (`tools/scraper/`)

**Purpose:** Web scouting with Intelligent Manifest generation

**Input:**
```json
{
  "target_site": "FT"  // FT, Bloomberg, Technoz
}
```

**Output:**
```json
{
  "batch_id": "01J8XYZ...",
  "top_10": [{"ulid": "...", "title": "...", "summary": "..."}],
  "inventory": [{"ulid": "...", "title": "..."}],
  "total_count": 42
}
```

**Execution Flow:**
1. Validate target against `VALID_TARGET_NAMES`
2. Launch Botasaurus browser
3. Extract links (deduplicated)
4. Process articles (3 retry validation → 3 retry summary)
5. **Direct embeddings**: `snowflake_client.embed()` per article
6. Store in `scraped_articles` + `scraped_articles_vec`
7. LLM curation for Top 10
8. Atomic save of `top_10_{batch_id}.json`
9. Generate Intelligent Manifest
10. Persist to `broadcast_batches`

**Resume Capability:**
- Checks `job_items` for `validation_passed=True` and `summary_generated=True`
- If found: Skips scraping, reads existing articles, regenerates embeddings only

**Manifest Format:**
```
### Scout Intelligence Briefing
**Target Site:** FT
**Batch ID:** 01J8XYZ...

#### Top 10 Articles
1. **Title**
   *URL:* ...
   *Conclusion:* ...
   *ULID:* ...

#### Extended Inventory (Next 50)
- Title (ULID: ...)

⚠️ NOTICE: Use batch_reader for remaining articles
```

### 4.2 Draft Editor (`tools/draft_editor/`)

**Purpose:** Atomic Top-10 list modification (SWAP-only)

**Input:**
```json
{
  "batch_id": "01J8XYZ...",
  "operations": [
    {"index_top10": 0, "target_identifier": "01J8ABC..."}  // ULID or index
  ]
}
```

**Output:**
```json
{
  "batch_id": "...",
  "status": "SUCCESS",
  "top_10": [...]
}
```

**Constraints:**
- **Status validation**: Only allows modification if `broadcast_batches.status == 'PENDING'`
- **Returns error**: If status is `PUBLISHING`, `PARTIAL`, `COMPLETED`, or `FAILED`
- **Atomic writes**: Uses `tempfile.NamedTemporaryFile` + `os.replace`

**Operations:**
1. **Internal SWAP**: Swap two items within Top-10 (by index)
2. **External SWAP**: Replace item with inventory article (by ULID)

**Validation:**
```python
if row["status"] != "PENDING":
    return json.dumps({
        "status": "FAILED", 
        "error": f"Cannot modify batch {batch_id} because its status is {row['status']}."
    })
```

### 4.3 Batch Reader (`tools/batch_reader/`)

**Purpose:** Semantic search filtered by batch

**Input:**
```json
{
  "batch_id": "01J8XYZ...",
  "query": "semiconductor supply chain",
  "limit": 5
}
```

**Output:**
```json
{
  "batch_id": "...",
  "query": "...",
  "results": [
    {
      "ulid": "...",
      "title": "...",
      "summary": "...",
      "conclusion": "...",
      "similarity": 0.923
    }
  ]
}
```

**Execution:**
1. Validate `sqlite_vec` availability
2. Read batch raw JSON to extract valid ULIDs
3. Generate query embedding via `generate_embedding()`
4. Execute filtered vector search:
```sql
SELECT a.title, a.summary, a.conclusion, a.id as ulid, (1 - v.distance) AS sim
FROM scraped_articles_vec v
JOIN scraped_articles a ON v.rowid = a.vec_rowid
WHERE v.embedding MATCH ? AND k = ?
  AND a.id IN (?, ?, ...)  -- Batch filter
ORDER BY v.distance ASC
```
5. Return top N results

### 4.4 Publisher (`tools/publisher/`)

**Purpose:** Translation and Telegram delivery

**Input:**
```json
{
  "batch_id": "01J8XYZ..."
}
```

**Output:**
```json
{"status": "SUCCESS", "message": "Batch ... published successfully."}
```

**Execution Flow:**
1. Check `broadcast_batches.status` → early return if `COMPLETED`
2. Set status to `PUBLISHING`
3. Spawn `PublisherPipeline(batch_id, top_10, inventory, job_id)`
4. Run 3-phase pipeline
5. On success: Set status `COMPLETED`
6. On exception: Set status `PARTIAL`, re-raise

---

## 5. Database Architecture

### 5.1 Schema v3 Complete Structure

```sql
-- Core Tables
jobs                    -- Job lifecycle
job_items               -- Step tracking (JSON metadata)
job_logs                -- Structured logs
broadcast_batches       -- Batch metadata

-- Scraper Tables
scraped_articles        -- Raw content
scraped_articles_vec    -- Vector embeddings

-- PDF Processing
pdf_parsed_pages        -- Text content
pdf_parsed_pages_vec    -- Vector embeddings

-- Token Usage
token_usage             -- LLM cost tracking

-- Support Tables (10 more)
```

### 5.2 Migration System Architecture

**State Machine:**
```
BASE_SCHEMA_VERSION = 3
    ↓
Discover migrations (v004_*, v005_*, etc.)
    ↓
Check limit (MAX_MIGRATION_SCRIPTS = 3)
    ↓
If > 3: perform_auto_fold()
    ↓
run_migrations(conn)
    ├─> WAL checkpoint
    ├─> Backup to .bak
    ├─> BEGIN EXCLUSIVE
    ├─> Disable FK constraints
    ├─> Execute migrations (individual execute() calls)
    ├─> Enable FK constraints
    ├─> Validate FK integrity
    ├─> COMMIT
    └─> Safe rollback guard
```

**Auto-Fold Process:**
1. Count active migrations
2. If > 3, select oldest (sorted by version)
3. Extract tables definitions
4. Merge into `database/schemas/__init__.py`
5. Update `BASE_SCHEMA_VERSION`
6. Delete migration file
7. Refresh module state
8. Re-scan (now ≤ 3 migrations)

### 5.3 Job Lifecycle State Machine

```
QUEUED → RUNNING → COMPLETED/FAILED
         ↓
   INTERRUPTED (recovery)
         ↓
   PAUSED_FOR_HITL (manual)
         ↓
   ABANDONED (3 failures)
```

### 5.4 Job Items Tracking

**Granular State Pipeline:**
```
PENDING → RUNNING → COMPLETED/FAILED
```

**Step Types:**
- `translate` - Article translation
- `publish_briefing` - Top-10 delivery
- `publish_archive` - Full inventory delivery

**Metadata Structure (Version 4):**
```json
{
  "step": "translate",
  "ulid": "01J8ABC...",
  "retry": 2,
  "timestamp": "2026-04-17T03:45:00.123Z",
  "model": "gpt-5.4-mini",
  "is_top10": true,
  "error": "Timeout after 3 attempts"
}
```

### 5.5 Auto-Repair Dictionary

**17 Tables with Repair Scripts:**

Example for `job_items`:
```python
"job_items": """
    CREATE TABLE job_items (
        item_id TEXT PRIMARY KEY,
        job_id TEXT,
        item_metadata TEXT,
        status TEXT,
        input_data TEXT,
        output_data TEXT,
        updated_at TEXT
    );
    CREATE INDEX idx_job_items_job ON job_items(job_id);
    CREATE INDEX idx_job_items_meta_step ON job_items(json_extract(item_metadata, '$.step'));
    CREATE INDEX idx_job_items_meta_ulid ON job_items(json_extract(item_metadata, '$.ulid'));
"""
```

**Fallback Pattern:**
- If `sqlite-vec` unavailable → BLOB storage
- If table missing → Auto-repair on first access
- If schema mismatch → Requires explicit reset

---

## 6. Data Flows & Pipelines

### 6.1 Scraper → Publisher Pipeline

```
1. Scraper creates batch
   ↓
2. Batch stored in broadcast_batches (PENDING)
   ↓
3. Publisher tool invoked
   ↓
4. Translation phase (job_items tracking)
   ↓
5. Briefing delivery (Top-10)
   ↓
6. Archive delivery (Full inventory)
   ↓
7. Status: COMPLETED
```

### 6.2 Resume Capability Flow

**Scraper Resume:**
```python
existing = {
    (json_extract(item_metadata, '$.step'), json_extract(item_metadata, '$.ulid'))
    for row in query_job_items
    if row['status'] == 'COMPLETED'
}

if ('validation', ulid) in existing and ('summary', ulid) in existing:
    # Skip scraping, regenerate embeddings only
    _emb = _sf.embed(article_text)
    _eb = struct.pack(...)
```

**Publisher Resume:**
```python
# Producer
translated = get_all_translated_items(job_id)  # Cached translations
if ulid in [t['ulid'] for t in translated]:
    continue  # Skip LLM call

# Consumer  
sent_briefing = query "pub_a_{ulid}"  # Sent messages
if found:
    continue  # Skip Telegram send
```

### 6.3 Migration Resume

**Auto-Fold Safety:**
- Non-recursive scan prevents infinite loops
- Module state refresh ensures fresh version reads
- Table deletion handled by assignment (not merge)

**Transaction Rollback:**
```python
try:
    # Migration executed
    conn.commit()
except Exception as e:
    # Restore from backup
    shutil.copy2(backup_path, db_path)
    # Safe rollback (guards against closed transaction)
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass  # Transaction already closed
    raise
```

### 6.4 AnythingLLM Callback

**Trigger:** Job completion (`COMPLETED` status)

**Endpoint:** `POST {ANYTHINGLLM_BASE_URL}/api/v1/workspace/{SLUG}/chat`

**Payload:**
```json
{
  "message": "TOOL_RESULT_CORRELATION_ID:{job_id}\n\n{result}",
  "mode": "chat",
  "attachments": [
    {
      "name": "top_10_01J8XYZ.json",
      "mime": "application/json",
      "contentString": "data:application/json;base64,..."
    }
  ],
  "reset": false
}
```

**Error Handling:** Failures logged, don't break worker.

---

## 7. Failure Modes & Safety

### 7.1 Error Recovery Matrix

| Failure Type | Detection | Recovery | Limit |
|-------------|-----------|----------|-------|
| Missing Table | `no such table` | Auto-repair DDL | 1 retry |
| Job Crash | Exception | `INTERRUPTED` → `QUEUED` | 3 strikes |
| Telegram API | HTTP error | Log + `PARTIAL` status | Bounded |
| Callback | HTTP error | Silent log | N/A |
| Schema Mismatch | Version check | Requires explicit reset | Manual |
| FK Constraint | SQL error | Abort + log details | Immediate |
| Migration Transaction | Exception | Restore backup + safe rollback | Auto |
| Stale Version Reference | Module state | Dynamic reload | Auto |

### 7.2 Sandboxing & Constraints

**Browser Operations:**
- Single `threading.Lock` prevents concurrent access
- Zombie chrome cleanup on startup
- Tab enforcement via `enforce_single_tab()`

**Database:**
- Single-writer prevents corruption
- WAL mode for concurrency
- BLOB fallback for missing vec0 extension
- **Migration transaction safety:**
  - `BEGIN EXCLUSIVE` for isolation
  - Individual `execute()` calls (no `executescript()`)
  - Backup before migration
  - Safe rollback guards

**Rate Limiting:**
- Telegram: 3.1s between messages
- LLM: Bounded by Azure deployment
- Browser: Lock-based serialization

### 7.3 Data Integrity

**Atomic Operations:**
- File writes: `tempfile` + `os.replace`
- DB writes: Single-writer thread
- JSON parsing: Validation with defaults
- **Migrations:** Transaction-level atomicity with rollback

**No Silent Failures:**
- Telegram sends return boolean
- Publisher sets `PARTIAL` on exception
- Job items track every step
- Logs include full context
- Migration failures restore backup

---

## 8. Configuration & Environment

### 8.1 Required Variables

```env
# API Security
API_KEY=dev_default_key_change_me_in_production
ANYTHINGTOOLS_PORT=8000

# AnythingLLM Integration
ANYTHINGLLM_API_KEY=...
ANYTHINGLLM_BASE_URL=http://localhost:3001
ANYTHINGLLM_WORKSPACE_SLUG=my-workspace

# Telegram (Publisher)
TELEGRAM_BOT_TOKEN=...
TELEGRAM_BRIEFING_CHAT_ID=...
TELEGRAM_ARCHIVE_CHAT_ID=...
TELEGRAM_MESSAGE_DELAY=3.1  # Rate limit

# Browser
CHROME_USER_DATA_DIR=chrome_profile

# Azure OpenAI (Publisher translations)
AZURE_KEY=...
AZURE_ENDPOINT=...
AZURE_DEPLOYMENT=gpt-5.4-mini

# Snowflake (Embeddings)
SNOWFLAKE_ACCOUNT=...
SNOWFLAKE_USER=...
SNOWFLAKE_WAREHOUSE=...
SNOWFLAKE_DATABASE=...
SNOWFLAKE_SCHEMA=...
SNOWFLAKE_PRIVATE_KEY_PATH=snowflake_private_key.p8

# Schema Management
SUMANAL_ALLOW_SCHEMA_RESET=0  # Set 1 for destructive migration

# Paths
ARTIFACTS_ROOT=artifacts
```

### 8.2 Optional Variables

```env
# Logging
TELEMETRY_DRY_RUN=false

# Job Watchdog
JOB_WATCH_INTERVAL_SECONDS=300
JOB_STALE_THRESHOLD_SECONDS=28800  # 8 hours

# Chutes (Alternative LLM provider)
CHUTES_API_TOKEN=...
CHUTES_MODEL=meta-llama/Llama-3.3-70B-Instruct
```

---

## 9. Installation & Deployment

### 9.1 Prerequisites

- Python 3.11+
- Playwright Chromium (`playwright install chromium`)
- Optional: `sqlite-vec` extension

### 9.2 Installation Steps

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Install browser
playwright install chromium

# 3. Configure environment
cp .env.example .env
# Edit .env with your credentials

# 4. Start application
uvicorn app:app --reload --port 8000
```

### 9.3 Database Initialization & Migration

**First Run:**
- Creates v3 schema from domain modules
- All 17 tables initialized via `database/schemas/`
- Migration runner activated
- Writer thread started

**Subsequent Runs:**
- Validates schema version via migrations
- Discovers new migration scripts
- Applies auto-fold if > 3 migrations exist
- Runs `run_migrations()` with safety mechanisms
- Validates foreign keys after migration

**Schema Migration:**
```bash
# For version upgrades (e.g., v3 → v4)
# 1. Create migration script: database/migrations/v004_descriptive_name.py
# 2. Ensure up() function uses individual execute() calls
# 3. Restart application - migrations auto-run
# 4. If > 3 migrations exist, oldest folds into BASE_SCHEMA_VERSION
```

**Emergency Rollback:**
```bash
# Migration failed? Database restored from .bak automatically
# Check logs for details
# Correct migration script and restart
```

### 9.4 Production Checklist

- [ ] Change `API_KEY` from default
- [ ] Set `TELEGRAM_MESSAGE_DELAY` >= 3.0
- [ ] Configure all credential variables
- [ ] Set `SUMANAL_ALLOW_SCHEMA_RESET=0`
- [ ] Mount `artifacts/` directory
- [ ] Configure log rotation
- [ ] Monitor `logs/` directory
- [ ] Verify `sqlite-vec` availability (optional)
- [ ] **Migration system:** Test auto-fold with ≥3 migrations
- [ ] **Transaction safety:** Verify backup/restore on failure
- [ ] **Version alignment:** Confirm `BASE_SCHEMA_VERSION` matches modules

---

## 10. Testing & Validation

### 10.1 E2E Tests

**`tests/test_browser_e2e.py`:**
- Minimal browser health check
- Verifies Chrome launch
- Checks Google navigation

**`tests/test_migration_pipeline.py` (NEW, outlined):**
- **Spec 1:** Migration discovery and validation
- **Spec 2:** Transaction rollback simulation
- **Spec 3:** Auto-fold with 4 migrations
- **Spec 4:** Version alignment verification
- **Spec 5:** Vector index preservation

### 10.2 Manual Validation

**Schema Check:**
```sql
PRAGMA user_version;  -- Should return current migration version
```

**Migration Status:**
```bash
# List migrations
ls database/migrations/v*.py

# Verify registry
python -c "from database.schemas import BASE_SCHEMA_VERSION, get_init_script; print(BASE_SCHEMA_VERSION)"
```

**Tool Registry:**
```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/api/manifest
# Should return 4 tools
```

**Metrics:**
```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/api/metrics
# Should show write_queue_size, active_jobs, registered_tools
```

**Job Lifecycle:**
```bash
# 1. Enqueue scraper
curl -X POST -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"args": {"target_site": "FT"}}' \
  http://localhost:8000/api/tools/scraper

# 2. Check status
curl -H "X-API-Key: $API_KEY" \
  http://localhost:8000/api/jobs/{job_id}

# 3. Monitor logs
tail -f logs/application.log
```

### 10.3 Database Inspection

**Check Migration Success:**
```sql
-- Verify old column removed
SELECT name FROM pragma_table_info('job_items') WHERE name = 'step_identifier';
-- Should return empty

-- Verify new column exists
SELECT name FROM pragma_table_info('job_items') WHERE name = 'item_metadata';
-- Should return item_metadata

-- Verify indexes
SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='job_items';
-- Should show idx_job_items_meta_step, idx_job_items_meta_ulid
```

**Inspect Job Items:**
```sql
-- View parsed metadata
SELECT 
    item_id,
    json_extract(item_metadata, '$.step') as step,
    json_extract(item_metadata, '$.ulid') as ulid,
    json_extract(item_metadata, '$.retry') as retry,
    json_extract(item_metadata, '$.is_top10') as is_top10,
    status
FROM job_items
WHERE job_id = '01J8XYZ...';
```

**Verify Auto-Fold:**
```sql
-- Check BASE_SCHEMA_VERSION updated
-- In Python: from database.schemas import BASE_SCHEMA_VERSION; print(BASE_SCHEMA_VERSION)

-- Check migration directory (should be ≤ 3 files)
-- ls database/migrations/
```

---

## 11. Monitoring & Operations

### 11.1 Key Metrics

**Application Metrics (`GET /metrics`):**
- `write_queue_size`: Number of pending DB writes
- `active_jobs`: Currently running jobs
- `registered_tools`: Tools loaded in registry
- `schema_version`: Current DB schema

**Job Metrics:**
- Queue depth (`SELECT COUNT(*) FROM jobs WHERE status='QUEUED'`)
- Success rate (COMPLETED / TOTAL)
- Average duration
- Retry counts per step

**Migration Metrics:**
- Active migration count
- Last fold timestamp
- Transaction success/failure rate

**Resource Metrics:**
- WAL file size
- Lock wait times
- Thread pool utilization

### 11.2 Log Structure

**Dual Logging (Console + File):**
```
logs/
├── application.log      # Main application
├── database.log         # DB operations (+ migrations)
├── scraper.log          # Scraper tool
└── publisher.log        # Publisher pipeline
```

**Log Format:**
```
[TIMESTAMP] [LEVEL] [TAG] MESSAGE | payload: {...}
```

**Critical Tags:**
- `DB:Repair` - Schema auto-repair
- `DB:Recovery` - Job resumption
- `DB:Migration` - Migration execution
- `Worker:Job:Crash` - Job failure
- `Publisher:Send` - Telegram delivery
- `Worker:Callback` - AnythingLLM callback
- `Migration:Fold` - Auto-fold process
- `Migration:Rollback` - Transaction rollback

### 11.3 Health Checks

**Startup Health:**
1. Vec0 extension loaded (or fallback)
2. Chrome launchable
3. DB writer started
4. **Migration system loaded**
5. **Schema version validated**
6. Registry loaded

**Runtime Health:**
1. Writer queue not growing
2. No stuck jobs (RUNNING > 24h)
3. Telegram rate limit respected
4. Callback endpoint reachable
5. **Migration count ≤ 3**
6. **Transaction log clean**

---

## 12. Known Limitations & Non-Goals

### 12.1 Explicit Limitations

| Limitation | Reason | Workaround |
|-----------|--------|------------|
| **Single-writer DB** | SQLite limitation | N/A (by design) |
| **4 tools only** | Lockdown architecture | Add to whitelist manually |
| **No concurrency** | Database integrity | Job-level parallelism |
| **Manual schema migration** | Data safety | `SUMANAL_ALLOW_SCHEMA_RESET` |
| **Bounded auto-repair** | Prevent infinite loops | Manual intervention required |
| **No embedded vector search** | Extension dependency | BLOB fallback mode |
| **Silent callback failures** | Don't break worker | Monitor logs |
| **Migration file limit (3)** | Prevent uncontrolled growth | Auto-folding mechanism |
| **Transaction breaks if executescript()** | SQLite auto-commit | Individual execute() calls |

### 12.2 Non-Goals (Wontfix)

- **Autonomous agent loops** → Architecture is deterministic
- **Dynamic tool loading** → Security lockdown
- **Real-time streaming** → Batch-oriented design
- **Multi-tenant isolation** → Single-session focus
- **Automatic schema upgrades** → Requires explicit consent
- **Infinite retry** → Bounded retry prevents cascading failures
- **Complex migration dependencies** → Linear versioning only

### 12.3 Design Rationale

**Why "Deterministic"?**
- Predictable execution path
- No uncontrolled LLM interaction
- Clear state transitions
- Resume without side effects

**Why "Single-writer"?**
- Prevents database corruption
- Simplifies concurrency model
- Enables WAL checkpointing
- Forces clear write boundaries

**Why "Whitelist"?**
- Security lockdown
- Resource control
- Quality assurance
- Support boundaries

**Why "Migration System"?**
- **Domain segregation:** Monolithic schema → maintainable modules
- **Autonomous management:** No manual intervention required
- **Transaction safety:** BEGIN EXCLUSIVE + rollback guards prevent corruption
- **Version discipline:** 3-file limit forces cleanup
- **Environment agnostic:** Works across dev/staging/production