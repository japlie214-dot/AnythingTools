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
- Single-writer SQLite with WAL mode.
- Tables: `jobs`, `broadcast_batches`, `scraped_articles`.

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
│   ├── connection.py               # Thread-local connections (WAL settings)
│   ├── writer.py                   # Background async writer
│   ├── schema.py                   # DB Initialization (Jobs, Articles, Batches)
│   └── job_queue.py                # Job status management
└── utils/
    ├── logger/                     # Dual logging (Console + File)
    ├── browser_lock.py             # Async lock for browser operations
    ├── hitl.py                     # Human-in-the-loop (Pause/Cancel logic)
    └── telegram_publisher.py       # Producer-Consumer pipeline for messages
```

**Key Structural Notes:**
- **`deprecated/`**: Contains all non-core logic. These files are not imported or used by the runtime engine.
- **`tools/`**: Contains only the 4 active tools. No dynamic scanning occurs.
- **`bot/engine/`**: Replaces `bot/core/agent.py` as the execution path.

## 4. Core Concepts & Domain Model

### Whitelisted Tools
The registry supports only:
1.  `scraper` (Scout): Outputs `batch_id`, `top_10`, `inventory`, `total_count`.
2.  `draft_editor` (Editor): Modifies curated JSON files. No LLM use.
3.  `batch_reader` (Reader): Filters vector search by `batch_id`. Outputs structured JSON.
4.  `publisher` (Herald): Translates content and posts to Telegram.

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

1.  **Worker Loop**: `UnifiedWorkerManager._run_loop()` sleeps 1s, polls `jobs` for `QUEUED` status.
2.  **Job Execution**:
    - `UnifiedWorkerManager._run_job()` extracts args.
    - `REGISTRY.create_tool_instance()` instantiates the specific tool class.
    - `run_tool_safely()` calls `tool.run()`.
    - **Scraper**: Performs browser action, saves JSON to disk, writes DB entries.
    - **Draft Editor**: Reads JSON, performs swap, writes atomically.
    - **Batch Reader**: Reads file, builds SQL `IN (...)` query, executes vector search.
    - **Publisher**: Spawns `PublisherPipeline`, where `consumer` waits for `queue.get()` and sends HTTP POSTs to Telegram with `time.sleep`.
3.  **Callback**:
    - Worker calls `_invoke_anythingllm_callback(job_id, result, attachments)`.
    - Reads files, encodes to Base64.
    - POSTs to configured URL.
4.  **Finish**: Job marked `COMPLETED`.

### Error Handling
- Tool execution errors are caught by `run_tool_safely` and returned as string output (failure message).
- HTTP errors (Telegram/Callback) are logged.
- Browser failures may raise exceptions caught by the worker.

### Configuration Paths
- `ANYTHINGLLM_BASE_URL`: Required for callback.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ARCHIVE_CHAT_ID`: Required for Publisher.
- `CHROME_USER_DATA_DIR`: Used by browser tools.

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
  }
  ```

### 6.1 Tool-Specific Arguments

#### **scraper**
```json
{
  "args": {
    "target_site": "FT"  // One of: "FT", "Bloomberg", "Technoz"
  }
}
```
**Returns**: JSON string with `{"batch_id": "...", "top_10": [...], "inventory": [...], "total_count": N}`

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
**Returns**: `"Success"` or raises `ValueError`

#### **publisher**
```json
{
  "args": {
    "batch_id": "01J8XYZ..."
  }
}
```
**Returns**: `"Publisher Pipeline Complete."` or error message

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
**Side Effect**: Returns `{error: "Vector search unavailable..."}` if `sqlite_vec` extension is missing.

### Tool Interface
All tools inherit `tools.base.BaseTool`:
```python
async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
    # Returns JSON string or error message
    # kwargs includes: job_id, session_id, cancellation_flag (threading.Event)
```

## 7. State, Persistence, and Data

### Storage
- **SQLite**: `data/sumanal.db` (WAL enabled).
- **Artifacts**: `artifacts/` (Scraper raw/curated JSON).
- **Logs**: `logs/` (Dual stream).

### Key Tables
- `jobs`: Execution lifecycle (QUEUED -> RUNNING -> COMPLETED).
- `broadcast_batches`: Links batch IDs to file paths.
- `scraped_articles`: Raw content for vector search.

### Data Lifecycle
- Jobs are retained indefinitely.
- Log files rotate.
- WAL files persist.

## 8. Dependencies & Integration

- **`botasaurus`**: Wrapper for Playwright used by Scraper.
- **`httpx`**: Async HTTP used by Publisher and Callback.
- **`openai`**: used *only* by `publisher` (internal translation step).
- **`sqlite3`**: Core persistence.
- **`python-telegram-bot`**: Telegram delivery.

## 9. Setup, Build, and Execution

### Prerequisites
- Python 3.11+
- `PLAYWRIGHT_BROWSERS_PATH` (via `playwright install chromium`).

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
    ```
4. Run: `uvicorn app:app --reload --port 8000`

## 10. Testing & Validation

- **`tests/test_browser_e2e.py`**: Minimal browser check.
- No formal unit test suite exists for the deterministic tools.
- Validation is achieved by inspecting the `jobs` table and `logs/` directory.

## 11. Known Limitations & Non-Goals

- **Strict Whitelist**: Only 4 tools exist.
- **No Autonomous Logic**: The engine waits for specific instructions.
- **No Legacy Features**: Finance, Research, etc., are in `deprecated/` and cannot run.
- **No Concurrency**: Single-writer DB prevents concurrent job execution per session.

## 12. Change Sensitivity

### Fragile Components
- **`tools/registry.py`**: Modifying the hardcoded list risks loading non-existent or deprecated tools.
- **`bot/engine/worker.py`**: The `AnythingLLM` callback payload format must match the receiving system. The cancellation flag propagation logic is critical.
- **`utils/telegram_publisher.py`**: Rate limiting logic is critical to avoid API bans. The producer-consumer coordination handles failure gracefully but requires the `try/except` pattern to be maintained.
- **`utils/browser_lock.py`**: Mixing `asyncio.Lock` with threading causes RuntimeErrors. Must remain as `threading.Lock`.
- **`database/schema.py`**: The `PAUSED_FOR_HITL` status constraint must be preserved in the jobs CHECK constraint.