# AnythingTools - Deterministic Tool Hosting Service

## 1. Project Overview

**What the system does:**
This system functions as a deterministic tool-hosting service. It accepts HTTP API requests for specific tools, executes them in a stateless or state-managed manner, and returns results. It is designed to integrate with an external orchestrator (e.g., AnythingLLM) by providing a strictly defined set of capabilities.
The legacy autonomous agent architecture (UnifiedAgent) has been fully quarantined and disabled. The system no longer performs autonomous reasoning loops, dynamic tool discovery, or uncontrolled LLM interaction.

**Concrete operational capabilities:**
- **Scraper:** Programmatic web scouting with Intelligent Manifest generation and structured JSON output.
- **Draft Editor:** Atomic list management for curation (Top 10 swaps and replacements).
- **Batch Reader:** Vector search queries restricted to specific batch IDs.
- **Publisher:** Translation and delivery via a Producer-Consumer pipeline with rate limiting.
- **PDF Processing:** PDF parsing with vector embedding storage and semantic search.

**What it explicitly does NOT do:**
- **No Autonomous Agents:** Does not contain an agent loop, reasoning engine, or dynamic persona switching.
- **No Dynamic Tool Loading:** Only the four whitelisted tools are executable.
- **No Legacy Features:** Finance analysis, research agents, polymarket trackers, quiz generators, and vector memory have been removed from the active execution path.
- **No LLM Integration (Direct):** The core engine does not initiate LLM calls (except within the specific Publisher pipeline for translation).

## 2. High-Level Architecture

The system has been refactored from an autonomous agent loop into a direct execution engine.

### Major Components

**API Layer (`api/`):**
- `routes.py`: Exposes endpoints to enqueue jobs and retrieve status.
- `schemas.py`: Pydantic models for the four core tools.

**Execution Engine (`bot/engine/`):**
- `worker.py`: The core poller. It queries the database for QUEUED jobs and executes them **directly**.
- `tool_runner.py`: Safety wrapper for tool execution (error handling).

**Tool System (`tools/`):**
- `registry.py`: **Lockdown Mode**. Hardcoded whitelist allowing only `scraper`, `draft_editor`, `publisher`, `batch_reader`.
- `base.py`: Abstract base class for tools.
- **Core Tools:**
  - `scraper`: Browsers automation + analysis.
  - `draft_editor`: JSON file manipulation.
  - `batch_reader`: SQL vector search.
  - `publisher`: Async pipeline for Telegram delivery.

**AnythingLLM Integration (`bot/engine/worker.py`):**
- Implements `_invoke_anythingllm_callback`.
- Sends HTTP POST requests to `ANYTHINGLLM_BASE_URL`.
- Payload includes JSON result and Base64-encoded file attachments.

**Database (`database/`):**
- **Single-writer SQLite with WAL mode** (v3 schema with auto-repair capability).
- **Automatic Schema Recovery**: Missing tables trigger DDL repair without manual intervention.
- **Comprehensive Table Repair Dictionary**: All 17 core tables can be repaired automatically.
- Tables: `jobs`, `broadcast_batches`, `scraped_articles`, `pdf_parsed_pages`, `token_usage`, etc.

**PDF Processing (`utils/pdf_utils.py`):**
- Embeds text chunks with Snowflake embeddings.
- Stores page content in `pdf_parsed_pages` (id INTEGER, chat_id INTEGER, pdf_name TEXT, content TEXT).
- Stores vectors in `pdf_parsed_pages_vec` (rowid INTEGER PRIMARY KEY, embedding BLOB).
- Supports SQLite vector search with automatic fallback to BLOB storage when vec0 extension unavailable.

### Data Flow (Direct Invocation)

1.  **API Request:** `POST /tools/{tool_name}` creates a job entry in `jobs` table (Status: QUEUED).
2.  **Worker Polling:** `UnifiedWorkerManager` polls the `jobs` table.
3.  **Direct Execution:**
    - Worker creates a tool instance via `REGISTRY.create_tool_instance()`.
    - Worker executes `tool.run(args, telemetry)`.
    - **No LLM reasoning loop occurs here.**
4.  **Result Processing:**
    - Tool output is captured.
    - `_invoke_anythingllm_callback` is called to POST results back to the external system.
5.  **Completion:** Job status updated to `COMPLETED`.

### Lifecycle
Event-driven polling (1-second interval). No long-running autonomous loops.

## 3. Repository Structure

```
AnythingTools/
├── app.py                          # FastAPI entry point (startup: auto-resume jobs)
├── config.py                       # Config (LLM/Telegram endpoints, Chrome profile)
├── README.md                       # This file
├── requirements.txt                # Dependencies
├── snowflake_private_key.p8        # (Unused in core, legacy artifact)
├── deprecated/                     # Quarantined Legacy Code
│   ├── bot/core/                   # UnifiedAgent (State Machine, Weaver, Modes)
│   └── tools/                      # Finance, Research, Polymarket, Quiz, etc.
├── api/
│   ├── routes.py                   # POST /tools, GET /jobs, GET /manifest
│   └── schemas.py                  # Input models for core tools
├── bot/engine/
│   ├── worker.py                   # Core execution loop + AnythingLLM Callback
│   └── tool_runner.py              # Error handling wrapper
├── tools/
│   ├── registry.py                 # Hardcoded Whitelist (4 tools)
│   ├── base.py                     # BaseTool interface
│   ├── scraper/                    # Web Scout (Botasaurus + Analysis)
│   ├── draft_editor/               # JSON Atomic Swap/Split
│   ├── batch_reader/               # Vector Search Filtered by Batch ID
│   └── publisher/                  # Translation + Telegram Producer-Consumer
├── database/
│   ├── connection.py               # Thread-local connections (WAL settings, SQLITE_VEC detection)
│   ├── writer.py                   # Background async writer with **Auto-Repair Logic**
│   ├── schema.py                   # DB Initialization + **Schema Repair Dictionary (v3)**
│   ├── job_queue.py                # Job status management
│   └── reader.py                   # Read-through cache with generation tracking
└── utils/
    ├── logger/                     # Dual logging (Console + File)
    ├── browser_lock.py             # Async lock for browser operations
    ├── hitl.py                     # Human-in-the-loop (Pause/Cancel logic)
    ├── telegram_publisher.py       # Producer-Consumer pipeline for messages
    ├── pdf_utils.py                # **PDF parsing with vec0/BLOB fallback**
    └── vector_search.py            # **Semantic search with rowid JOIN logic**
```

**Key Structural Notes:**
- **`deprecated/`**: Contains all non-core logic. These files are not imported or used by the runtime engine.
- **`tools/`**: Contains only the 4 active tools. No dynamic scanning occurs.
- **`bot/engine/`**: Replaces `bot/core/agent.py` as the execution path.
- **Database Layer**: Now includes robust self-healing capabilities for missing or corrupted schemas.

## 4. Core Concepts & Domain Model

### Whitelisted Tools
The registry supports only:
1.  `scraper` (Scout): Outputs `batch_id`, `top_10`, `inventory`, `total_count`. Now with resume-capable embedding generation using direct `snowflake_client.embed()` call (no async wrapper).
2.  `draft_editor` (Editor): Modifies curated JSON files. Strictly SWAP-only operations. Rejects modifications when batch status is not `PENDING`.
3.  `batch_reader` (Reader): Filters vector search by `batch_id`. Outputs structured JSON with `ORDER BY v.distance ASC` for accurate semantic ranking.
4.  `publisher` (Herald): Translates content and posts to Telegram. Full resume capability via `job_items` caching with `PUBLISHING`/`PARTIAL`/`COMPLETED` batch states.

### Job Items State Tracking
The `job_items` table enables granular resume capability:
- **Step identifiers**: `trans_{ulid}` for translations, `pub_a_{ulid}` and `pub_b_{ulid}` for delivery tracking
- **Status tracking**: `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`
- **Data persistence**: `input_data` and `output_data` fields store state

### Publisher Pipeline State Management
**Parallel to `broadcast_batches.status`**:
- `PENDING`: Batch exists, not yet published
- `PUBLISHING`: Active publishing in progress (set before pipeline start)
- `PARTIAL`: Publishing failed mid-stream (set on exception)
- `COMPLETED`: All messages delivered successfully (set on complete)

### Database Schema Versioning & Auto-Repair
**Version: 3** (Current)

**Automatic Recovery Mechanism:**
When a "no such table" error occurs during database operations:
1. Table name is extracted using robust regex: `r'no such table:\s*(?:\"|[\w\.]+\.)?(\w+)\"?'`
2. Repair script is fetched from `TABLE_REPAIR_SCRIPTS` dictionary
3. DDL is executed (schema only, no data re-execution)
4. Original query is retried once (bounded by `MAX_REPAIR_RETRIES = 1`)

**Schema Mismatch Resolution:**
- **Original Issue**: `pdf_parsed_pages.id` was TEXT, causing JOIN failures
- **Fixed in v3**: Changed to INTEGER to match `pdf_parsed_pages_vec.rowid`
- **Impact**: Prevents type coercion errors during vector search

**PDF Processing Schema:**
```sql
CREATE TABLE IF NOT EXISTS pdf_parsed_pages (
    id INTEGER NOT NULL PRIMARY KEY,  -- Changed from TEXT (see Changes section)
    chat_id INTEGER,
    pdf_name TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    content TEXT,
    embedding_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(embedding_status IN ('PENDING','EMBEDDED','SKIPPED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE IF NOT EXISTS pdf_parsed_pages_vec USING vec0(
    embedding float[1024]
);
-- Fallback (no sqlite_vec): rowid INTEGER PRIMARY KEY AUTOINCREMENT, embedding BLOB
```

**Vector Search Pattern:**
- Uses JOIN on `rowid` (vec table) = `CAST(id AS INTEGER)` (main table)
- Supports both real vec0 virtual tables and BLOB fallbacks
- All vec INSERT operations use `(rowid, embedding)` pattern

### Batch Management
- **`broadcast_batches` table**: Stores `batch_id` and file paths (`raw_json_path`, `curated_json_path`).
- **Scope Isolation**: `batch_reader` specifically filters `ids` found in the raw JSON file to ensure search results are restricted to that batch.

### Atomic File Operations
- `draft_editor` utilizes `tempfile.NamedTemporaryFile` and `os.replace` to ensure file integrity during writes.

### Producer-Consumer Pattern
- `publisher` utilizes `asyncio.Queue` to translate articles (Producer) and send them to Telegram (Consumer) with strict rate limiting (`TELEGRAM_MESSAGE_DELAY`).

### AnythingLLM Callback Contract
- **Trigger**: Job completion.
- **Method**: HTTP POST.
- **Destination**: `config.ANYTHINGLLM_BASE_URL`.
- **Payload**: JSON result + Base64 attachments.
- **Correlation**: Includes `TOOL_RESULT_CORRELATION_ID:{job_id}` in the message body.

## 5. Detailed Behavior

### Normal Execution Flow (Direct)

1.  **Worker Loop**: `UnifiedWorkerManager._run_loop()` sleeps 1s, polls `jobs` for `QUEUED` status. Prioritizes `INTERRUPTED` jobs for recovery.
2.  **Job Execution**:
    - `UnifiedWorkerManager._run_job()` extracts args and creates `cancellation_flag` (threading.Event).
    - `REGISTRY.create_tool_instance()` instantiates the specific tool class.
    - `run_tool_safely()` calls `tool.run()` with `job_id`, `session_id`, `cancellation_flag` kwargs.
    - **Scraper**: Performs browser action, saves JSON to disk, writes DB entries. **Resume behavior**: Embedding-only path reads from `scraped_articles` and uses direct `snowflake_client.embed()` + `struct.pack()`. No `generate_embedding_sync()` call.
    - **Draft Editor**: Reads JSON, performs swap, writes atomically. **Validation**: Checks `broadcast_batches.status == 'PENDING'`; rejects with error if not.
    - **Batch Reader**: Reads file, builds SQL `IN (...)` query with `ORDER BY v.distance ASC`, executes vector search.
    - **Publisher**: Spawns `PublisherPipeline(batch_id, top_10, inventory, job_id)`.
      - **Producer**: Checks `job_items` for `trans_{ulid}` with `COMPLETED` status. Skips cached translations. Writes new translations to `job_items`.
      - **Consumer**: Checks `job_items` for `pub_a_{ulid}` / `pub_b_{ulid}`. Skips already-sent messages. Writes delivery status to `job_items`.
      - **Batch Status**: Sets `PUBLISHING` → `PARTIAL` (on exception) or `COMPLETED` (on success) in `broadcast_batches`.
    - **PDF Processing**: Extracts text, generates embeddings, stores in `pdf_parsed_pages` + `pdf_parsed_pages_vec`.
3.  **Callback**:
    - Worker calls `_invoke_anythingllm_callback(job_id, result, attachments)`.
    - Reads files, encodes to Base64.
    - POSTs to configured URL with `TOOL_RESULT_CORRELATION_ID:{job_id}`.
4.  **Finish**: Job marked `COMPLETED`.

### Resume Behavior (INTERRUPTED Jobs)

**Startup Recovery** (line 284-296 in `app.py`):
- Automatically requeues `RUNNING` and `INTERRUPTED` jobs to `QUEUED` status on startup.

**Publisher Pipeline Resume**:
- **Translation Cache**: `producer()` queries `job_items` for `status='COMPLETED' AND step_identifier='trans_{ulid}'`. If found, deserializes `output_data` and skips LLM call.
- **Delivery Deduplication**: `consumer()` queries `pub_a_{ulid}` / `pub_b_{ulid}` before sending. Prevents duplicate Telegram messages.
- **Batch State**: PublisherTool checks `broadcast_batches.status`; returns early if `COMPLETED`.

**Scraper Resume**:
- **Embedding-Only Path**: When `validation_passed=True` and `summary_generated=True` in `job_items` metadata, reads existing `scraped_articles` and re-generates embedding only using direct Snowflake client call:
  ```python
  _emb = _sf.embed(_et)
  _eb = _struct.pack(f"{len(_emb)}f", *_emb)
  ```

**Draft Editor Protection**:
- Explicitly rejects `status != 'PENDING'` batches to preserve Top-10 cardinality.
- Returns JSON error: `{"status": "FAILED", "error": "Cannot modify batch {id} because its status is {status}."}`

### Error Handling & Auto-Repair
- Tool execution errors are caught by `run_tool_safely` and returned as string output (failure message).
- HTTP errors (Telegram/Callback) are logged.
- Browser failures may raise exceptions caught by the worker.
- **Database Auto-Repair**: When `no such table` errors occur:
  - Regex extracts table name from error message
  - DDL script fetched from `TABLE_REPAIR_SCRIPTS`
  - Schema repaired without data loss
  - Original operation retried once
- **Foreign Key Constraint Failures**: Immediately abort with detailed payload logging (includes params for debugging).
- **Retry Logic**: Bounded to `MAX_REPAIR_RETRIES = 1` to prevent infinite loops.

### Configuration Paths
- `ANYTHINGLLM_BASE_URL`: Required for callback.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ARCHIVE_CHAT_ID`: Required for Publisher.
- `CHROME_USER_DATA_DIR`: Used by browser tools.
- `SUMANAL_ALLOW_SCHEMA_RESET`: Boolean flag to enable destructive schema resets (default: false).

## 6. Public Interfaces

### REST API

All endpoints are exposed at `/api` prefix.

#### **Enqueue Job**
- **Path**: `POST /api/tools/{tool_name}`
- **Path Parameters**:
  - `tool_name`: string, one of `scraper`, `draft_editor`, `publisher`, `batch_reader`
- **Request Body**:
  ```json
  {
    "args": {
      // Tool-specific arguments (see Section 6.1)
    },
    "client_metadata": {
      // Optional metadata forwarded to tool
    }
  }
  ```
- **Response**: `202 Accepted`
  ```json
  {
    "job_id": "01J8XYZ...",
    "status": "QUEUED"
  }
  ```

#### **Get Job Status**
- **Path**: `GET /api/jobs/{job_id}`
- **Path Parameters**:
  - `job_id`: string, unique identifier returned by enqueue
- **Response**: `200 OK`
  ```json
  {
    "job_id": "01J8XYZ...",
    "status": "COMPLETED",
    "job_logs": [
      {
        "timestamp": "2026-04-17T03:45:00.123Z",
        "level": "INFO",
        "tag": "Scraper:Init",
        "status_state": "RUNNING",
        "message": "Starting extraction..."
      }
    ],
    "final_payload": {
      // Tool-specific result or error
    }
  }
  ```
- **Status Values**: `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLING`, `INTERRUPTED`, `PAUSED_FOR_HITL`, `ABANDONED`

#### **Cancel Job**
- **Path**: `DELETE /api/jobs/{job_id}`
- **Path Parameters**:
  - `job_id`: string, job to cancel
- **Response**: `202 Accepted`
  ```json
  {
    "job_id": "01J8XYZ...",
    "status": "CANCELLING"
  }
  ```
- **Behavior**: Sets job status, activates cancellation flag if job is running.

#### **Get Manifest**
- **Path**: `GET /api/manifest`
- **Response**: `200 OK`
  ```json
  {
    "tools": [
      {
        "name": "scraper",
        "input_model": { /* Pydantic schema */ }
      },
      {
        "name": "draft_editor",
        "input_model": { /* Pydantic schema */ }
      },
      {
        "name": "publisher",
        "input_model": { /* Pydantic schema */ }
      },
      {
        "name": "batch_reader",
        "input_model": { /* Pydantic schema */ }
      }
    ]
  }
  ```

#### **Metrics**
- **Path**: `GET /api/metrics`
- **Response**: `200 OK`
  ```json
  {
    "write_queue_size": 0,
    "active_jobs": 0,
    "registered_tools": 4
    "schema_version": 3
  }
  ```

### 6.1 Tool-Specific Arguments

#### **scraper**
```json
{
  "args": {
    "target_site": "FT"  // One of: "FT", "Bloomberg", "Technoz" (validated against VALID_TARGET_NAMES)
  }
}
```
**Returns**: JSON string with `{"batch_id": "...", "top_10": [...], "inventory": [...], "total_count": N}`
**Behavior**:
- Validates `target_site` against `VALID_TARGET_NAMES` (set of valid targets)
- Uses direct `snowflake_client.embed()` for embeddings (no `generate_embedding_sync()`)
- Resume-capable: `cancellation_flag` kwarg supported

#### **draft_editor**
```json
{
  "args": {
    "batch_id": "01J8XYZ...",
    "operations": [
      {
        "index_top10": 0,
        "target_identifier": "01J8ABC..."  // ULID or index from inventory
      }
    ]
  }
}
```
**Returns**: JSON string with `{"batch_id": "...", "status": "SUCCESS", "top_10": [...]}`
**Behavior**:
- Validates `broadcast_batches.status == 'PENDING'` before modification
- Returns error JSON if status is `PUBLISHING`, `PARTIAL`, `COMPLETED`, or `FAILED`

#### **publisher**
```json
{
  "args": {
    "batch_id": "01J8XYZ..."
  },
  "kwargs": {
    "job_id": "01J..."  // Optional, enables resume capability
  }
}
```
**Returns**: `{"status": "SUCCESS", "message": "Batch {batch_id} published successfully."}` or error
**Behavior**:
- Checks `broadcast_batches.status`; returns early if `COMPLETED`
- Sets status: `PUBLISHING` → `PARTIAL` (on failure) → `COMPLETED` (on success)
- **Resume**: Uses `job_items` to skip cached translations (`trans_{ulid}`) and duplicate delivery (`pub_a_{ulid}`, `pub_b_{ulid}`)

#### **batch_reader**
```json
{
  "args": {
    "batch_id": "01J8XYZ...",
    "query": "semiconductor supply chain",
    "limit": 5
  }
}
```
**Returns**: JSON string with `{"batch_id": "...", "query": "...", "results": [...]}`
**Behavior**:
- Search results ordered by `v.distance ASC` (most similar first)
- Filters by `batch_id` raw JSON file contents
- Returns error if `sqlite_vec` unavailable

### Tool Interface
All tools inherit `tools.base.BaseTool`:
```python
async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
    # Returns JSON string or error message
    # kwargs includes: job_id, session_id, cancellation_flag (threading.Event)
```

## 7. State, Persistence, and Data

### Storage
- **SQLite**: `data/sumanal.db` (WAL enabled, Schema Version 3).
- **Artifacts**: `artifacts/` (Scraper raw/curated JSON).
- **Logs**: `logs/` (Dual stream).

### Key Tables
- `jobs`: Execution lifecycle (QUEUED -> RUNNING -> COMPLETED).
- `broadcast_batches`: Links batch IDs to file paths.
- `scraped_articles`: Raw content for vector search.
- `pdf_parsed_pages`: PDF text content with INTEGER primary key.
- `pdf_parsed_pages_vec`: Vector embeddings (virtual or BLOB).

### Data Lifecycle
- Jobs are retained indefinitely.
- Log files rotate.
- WAL files persist.

### Schema Evolution
- **v2 to v3 Migration**: Destructive reset when `SUMANAL_ALLOW_SCHEMA_RESET=1`
- **Auto-Repair Operations**: Non-destructive schema corrections on missing tables
- **Vector Table Fallbacks**: Automatic BLOB table creation when vec0 unavailable

## 8. Dependencies & Integration

- **`botasaurus`**: Wrapper for Playwright used by Scraper.
- **`httpx`**: Async HTTP used by Publisher and Callback.
- **`openai`**: used *only* by `publisher` (internal translation step).
- **`sqlite3`**: Core persistence with version 3 schema.
- **`sqlite-vec`**: Optional extension for vector search (fallback to BLOB).
- **`python-telegram-bot`**: Telegram delivery.

## 9. Setup, Build, and Execution

### Prerequisites
- Python 3.11+
- `PLAYWRIGHT_BROWSERS_PATH` (via `playwright install chromium`).
- Optional: `sqlite-vec` extension for native vector search.

### Installation
1. `pip install -r requirements.txt`
2. `playwright install chromium`
3. Configure `.env`:
    ```env
    AZURE_KEY=...
    AZURE_ENDPOINT=...
    AZURE_DEPLOYMENT=...
    ANYTHINGLLM_BASE_URL=...  # Critical for Callback
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_ARCHIVE_CHAT_ID=...
    SUMANAL_ALLOW_SCHEMA_RESET=0  # Set to 1 for destructive v3 migration
    ```
4. Run: `uvicorn app:app --reload --port 8000`

### Database Initialization
- **First Run**: Creates v3 schema with all 17 tables
- **Subsequent Runs**: Validates version, applies auto-repair if needed
- **Migration**: Requires `SUMANAL_ALLOW_SCHEMA_RESET=1` for major version changes

## 10. Testing & Validation

- **`tests/test_browser_e2e.py`**: Minimal browser check.
- No formal unit test suite exists for the deterministic tools.
- Validation is achieved by inspecting the `jobs` table and `logs/` directory.
- **Schema Validation**: Check `PRAGMA user_version` returns 3

## 11. Known Limitations & Non-Goals

- **Strict Whitelist**: Only 4 tools exist.
- **No Autonomous Logic**: The engine waits for specific instructions.
- **No Legacy Features**: Finance, Research, etc., are in `deprecated/` and cannot run.
- **No Concurrency**: Single-writer DB prevents concurrent job execution per session.
- **Manual Migration Required**: Schema version changes require `SUMANAL_ALLOW_SCHEMA_RESET=1` (destructive).
- **Bounded Auto-Repair**: Only 1 retry per missing table (prevents infinite loops).

## 12. Change Sensitivity

### Fragile Components
- **`tools/registry.py`**: Modifying the hardcoded list risks loading non-existent or deprecated tools.
- **`bot/engine/worker.py`**: The `AnythingLLM` callback payload format must match the receiving system. The cancellation flag propagation logic is critical.
- **`utils/telegram_publisher.py`**: Rate limiting logic is critical to avoid API bans. The producer-consumer coordination handles failure gracefully but requires the `try/except` pattern to be maintained.
- **`utils/browser_lock.py`**: Mixing `asyncio.Lock` with threading causes RuntimeErrors. Must remain as `threading.Lock`.
- **`database/writer.py`**: Repair loop logic must maintain `MAX_REPAIR_RETRIES = 1` and include both FK and missing table detection.
- **`database/schema.py`**: `get_repair_script()` must return identical patterns to `get_init_script()` fallback replacements for consistency.

## 13. Changes (Evolutionary Analysis from Current Code)

This section identifies significant architectural refactors based on observable evidence within the current codebase. Each change is inferred from code patterns, deprecated file locations, and structural inconsistencies.

### 13.1 Publisher Pipeline Resume Capability

**Pain Point Addressed**: The publisher pipeline had no mechanism to recover from interruptions. If a job crashed during translation or delivery, it would restart from zero, causing duplicate LLM API calls (expensive) and duplicate Telegram messages (poor user experience).

**Solution Implemented**:
1. **`utils/telegram_publisher.py`**:
   - Added `job_id: str | None = None` parameter to `PublisherPipeline.__init__()` (line 21)
   - `producer()` queries `job_items` for `trans_{ulid}` entries with `COMPLETED` status before calling LLM
   - `consumer()` queries `pub_a_{ulid}` / `pub_b_{ulid}` to prevent duplicate Telegram sends
   - Both methods call `add_job_item()` and `update_item_status()` to persist progress

2. **`tools/publisher/tool.py`**:
   - `run()` method accepts `job_id` from kwargs
   - Manages `broadcast_batches.status` lifecycle: `PUBLISHING` → `PARTIAL`/`COMPLETED`
   - Passes `job_id` to pipeline and checks for `COMPLETED` status to skip already-published batches

**Evidence**: Direct code additions in publisher files show explicit `job_items` queries and status tracking.

### 13.2 Scraper Embedding Generation Refactor

**Pain Point Addressed**: `utils/vector_search.py` contained `generate_embedding_sync()` with complex event loop detection logic, redundant with async path, causing issues in resume paths.

**Solution Implemented**:
- **Deleted**: `generate_embedding_sync()` from `utils/vector_search.py` (23 lines removed)
- **Modified**: `tools/scraper/task.py` resume path directly uses `snowflake_client.embed()` + `struct.pack()`

**Evidence**: Function deletion from file and direct synchronous calls in scraper task resume path.

### 13.3 Draft Editor State Enforcement (Swap-Only Constraint)

**Pain Point Addressed**: Draft editor could modify batches mid-publication, causing race conditions and breaking Top-10 cardinality.

**Solution Implemented**:
- **Modified**: `tools/draft_editor/tool.py` `run()` method checks `broadcast_batches.status == 'PENDING'`
- Returns error JSON if status is `PUBLISHING`, `PARTIAL`, `COMPLETED`, or `FAILED`

**Evidence**: Direct status check addition with explicit error return.

### 13.4 Batch Reader Vector Ordering Fix

**Pain Point Addressed**: Results appeared in arbitrary order, making curation difficult.

**Solution Implemented**:
- **Modified**: `tools/batch_reader/tool.py` SQL query adds `ORDER BY v.distance ASC`

**Evidence**: SQL modification in batch reader file.

### 13.5 Infrastructure Pre-existing

The base resume capability exists but was extended:
- **`app.py`** lines 284-296: Startup recovery scans for `RUNNING`/`INTERRUPTED` jobs
- **`bot/engine/worker.py`**: Handles `INTERRUPTED` status with recovery message
- **`tools/base.py`**: Resets `_last_artifacts` on each execution

**Inference**: Recent changes extended job-level recovery to granular `job_items` tracking specifically for publisher pipeline.

**Confidence Level**: **High** for all changes (direct code evidence exists in current files)

### Summary

Architectural evolution: Stateless execution → Resume-capable → Enforced constraints → Direct synchronous calls → Ranked results. The system matured toward reliability, cost-efficiency, and user safety.