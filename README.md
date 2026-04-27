# AnythingTools - Modular Tool Hosting service with Advanced Startup Architecture

## 1. Project Overview

AnythingTools is a FastAPI-based deterministic tool hosting service that provides web scraping, publishing, batch reading, and backup capabilities via a REST API. The system executes tools in isolated threads with a single-writer database architecture (SQLite WAL mode) and structured callback delivery.

### Operational Capabilities

- **Web Scraper**: DOM-validated extraction, ULID-based identification, automatic delta backup post-persistence, configurable target site registry
- **Publisher**: Telegram message delivery with state management and crash recovery
- **Batch Reader**: Hybrid semantic search combining vector embeddings (FTS5) and full-text search
- **Backup System**: Streaming Parquet export/import with OOM-safe chunking (500 rows/batch), watermark-based delta, FTS5 post-restore rebuild

### Explicit Non-Capabilities

- **No continuous/real-time backup**: Batch-only execution, manual or triggered
- **No selective restore**: All-or-nothing restoration for master tables only
- **No telemetry**: Local SQLite only, no external metrics collection
- **No concurrent writers**: Single background writer thread (max 1000 queued tasks)
- **No automatic schema migration**: Manual reconciliation via reconciler
- **No backup verification**: No checksums or corruption detection
- **No FTS backup**: FTS tables excluded from restores, rebuilt synchronously

---

## 2. High-Level Architecture

### Core Components

**1. API Layer (`app.py`, `api/`)**
- FastAPI lifespan manager for startup/shutdown orchestration
- Static file server mounted at `/artifacts` and `/api/artifacts`
- Background job execution for export/restore operations
- **Key Endpoints**:
  - `POST /api/tools/{tool}` - Enqueue tool execution
  - `GET /api/jobs/{id}` - Job status with logs
  - `POST /api/backup/export` - Manual backup trigger
  - `POST /api/backup/restore` - Manual restore (requires browser_lock)
  - `GET /api/backup/status` - Backup directory status
  - `GET /api/metrics` - System metrics (queue, active jobs)

**2. Startup Orchestration (`utils/startup/`)**
- **New modular architecture** (post-refactor):
  - `core.py`: StartupOrchestrator with concurrent tiering support
  - `cleanup.py`: Zombie Chrome process and temp file cleanup
  - `server.py`: Dynamic artifacts directory mounting from config
  - `database.py`: Pragmas, DB writer initialization, lifecycle runner, vec0 validation
  - `registry.py`: Tool registry loading with validation
  - `browser.py`: Browser warmup (5s wait → example.com navigation → verification)
  - `telegram.py`: Async orphan handshake for Telegram bot token
  - `__init__.py`: Pipeline assembly with three-tier execution

**3. Worker Manager (`bot/engine/worker.py`)**
- `UnifiedWorkerManager`: Polls database every 1s for `QUEUED`, `INTERRUPTED`, `PENDING_CALLBACK` jobs
- Thread-isolated tool execution with cancellation flags
- Callback delivery with exponential backoff (3 attempts max)
- **Job lifecycle**: `QUEUED` → `RUNNING` → `COMPLETED|FAILED|PARTIAL|PENDING_CALLBACK|INTERRUPTED`
- **Recovery**: Automatically requeues interrupted jobs on restart

**4. Database Layer (`database/`)**
- **Single-writer background thread** (`writer.py`) with bounded queue (max 1000)
- **WAL mode** for concurrent readers
- **Schema v9** with `updated_at` tracking for delta backups
- **Tables**:
  - *Master*: `scraped_articles`, `scraped_articles_vec`, `long_term_memories`, `long_term_memories_vec`
  - *Non-master*: `jobs`, `job_items`, `job_logs`, `broadcast_batches`
- **Schema Reconciliation** (`reconciler.py`): Detects drift, performs pre-drop snapshots, cascades FK recreations
- **FTS5 Handling**: Excluded from standard reconciliation, created via dedicated existence-based checks

**5. Tool Layer (`tools/`)**
- **Scraper**: Full pipeline (extraction → curation → persistence → auto-backup) with job_items tracking
- **Publisher**: Telegram delivery with state management via job_items
- **Batch Reader**: Hybrid vector + FTS5 search
- **Backup**: Multi-table Parquet export/import with streaming
- **Registry** (`registry.py`): Whitelisted core tools only: `scraper`, `draft_editor`, `publisher`, `batch_reader`

**6. Backup System (`tools/backup/`)**
- **Config**: OOM-safe batch size ceiling (10,000 rows)
- **Schema**: PyArrow schemas with binary embeddings (variable-length, was fixed)
- **Exporter** (`exporter.py`): Chunked 500-row SQL reads, parameterized queries, FTS exclusion
- **Storage** (`storage.py`): Atomic writes, embedding validation, ISO-8601 watermarks
- **Restore** (`restore.py`): Single-writer queue routing, adaptive column mapping, synchronous FTS rebuild
- **Runner** (`runner.py`): Read-only connection, ISO-8601 timestamps

### Execution Model

- **API**: Event-driven (FastAPI)
- **Worker**: Polling-based (1s interval)
- **Tools**: Thread-isolated execution
- **Database**: Single-writer, multi-reader (WAL)
- **Backup**: Streaming chunked execution (prevent OOM)

### Data Flow

**Startup Pipeline (Three-Tier):**
```
Tier 1 (Concurrent):
├─ mount_artifacts → config.ARTIFACTS_DIR
├─ cleanup_zombie_chrome → psutil.scan + kill
├─ cleanup_temp_files → remove *.tmp.parquet
├─ init_database_layer → pragmas + start_writer()
└─ start_telegram_handshake → async task launch

Tier 2 (Sequential):
├─ run_db_migrations → lifecycle.reconcile()
└─ validate_vec0 → verify extension / fallback

Tier 3 (Concurrent):
├─ load_tool_registry → whitelist + validation
└─ warmup_browser → wait 5s → navigate to example.com → verify text
```

**Backup Export (Full/Delta):**
```
[Reader Thread] → export_table_chunks(conn, table, mode, last_ts)
    ↓
[Chunked Iteration] → 500 rows → DataFrame
    ↓
[write_table_batch] → Embedding validation (vector tables only)
    ↓
[ParquetWriter] → Atomic .tmp → rename
    ↓
[Watermark Update] → table_watermarks: {table: ISO-8601}
```

**Restore with FTS Rebuild:**
```
[Read Connection] → restore_master_tables_direct(conn)
    ↓
[Per Table] → ParquetFile.iter_batches(500)
    ↓
[Statements Prep] → INSERT with adaptive column mapping
    ↓
[Single-Writer Queue] → enqueue_transaction(statements)
    ↓
[Wait] → wait_for_writes(120s timeout per table)
    ↓
[FTS Rebuild] → enqueue_write(FTS_REBUILD_SQL)
    ↓
[Wait] → wait_for_writes(300s timeout for rebuild)
```

---

## 3. Repository Structure

```
./
├── api/                      # FastAPI routes + schemas
│   ├── routes.py            # All endpoints with job/backup logic
│   ├── schemas.py           # Pydantic models (watermark support)
│   ├── telegram_client.py   # Bot API + orphan handshake
│   └── telegram_notifier.py # Message delivery
├── bot/                     # Worker engine
│   ├── engine/
│   │   ├── worker.py        # UnifiedWorkerManager (threads)
│   │   └── tool_runner.py   # Job execution helpers
│   └── core/
│       └── constants.py     # Job status enums
├── clients/                 # External services
│   ├── snowflake_client.py  # Snowflake connector
│   └── llm/
│       ├── factory.py       # LLM provider selection
│       └── payloads.py      # Request builders
├── database/                # SQLite layer
│   ├── schemas/             # Canonical DDL
│   │   ├── __init__.py      # MASTER_TABLES, ALL_FTS_TABLES
│   │   ├── vector.py        # FTS5 triggers + vec0 tables
│   │   └── *.py             # jobs, finance, pdf, token
│   ├── reconciler.py        # Schema drift detection + repair
│   ├── schema_introspector.py # PRAGMA parsing + DDL comparison
│   ├── lifecycle.py         # Reconciler wrapper + recovery
│   ├── writer.py            # Background single-writer thread
│   ├── connection.py        # DB manager (optional vec0, query_only)
│   ├── health.py            # Table validation
│   └── *.py                 # reader, job_queue, blackboard, formula_cache
├── deprecated/              # Legacy code (~70% volume, never loaded)
│   ├── bot/                 # Old agent/weaver/modes
│   └── tools/               # Old research, finance, polymarket, etc.
├── tools/                   # Tool implementations
│   ├── scraper/             # Extraction + curation + persistence
│   │   ├── prompts.py       # Canonical prompts (post-PLAN-02)
│   │   ├── Skill.py         # Tool descriptor
│   │   ├── tool.py          # Main tool
│   │   ├── browser.py       # DOM helpers
│   │   ├── curation.py      # Article selection
│   │   └── extraction.py    # Content extraction
│   ├── publisher/           # Telegram delivery
│   ├── batch_reader/        # Hybrid search
│   ├── backup/              # Hardened backup system
│   │   ├── config.py        # Batch ceiling (10k), OOM rules
│   │   ├── models.py        # Watermark/Result (Pydantic compat)
│   │   ├── schema.py        # PyArrow schemas, validation helpers
│   │   ├── exporter.py      # Parameterized queries, FTS exclusion
│   │   ├── storage.py       # Atomic writes + embedding validation
│   │   ├── restore.py       # enqueue_transaction + sync FTS
│   │   └── runner.py        # Read-only connection
│   ├── draft_editor/        # Content editing tool
│   ├── base.py              # BaseTool
│   └── registry.py          # Whitelisted tool discovery
├── utils/                   # Infrastructure
│   ├── startup/             # Modular startup system (NEW)
│   │   ├── core.py          # StartupOrchestrator (tiers)
│   │   ├── cleanup.py       # Zombie chrome + temp files
│   │   ├── server.py        # Artifacts mounting
│   │   ├── database.py      # Pragmas, writer, lifecycle, vec0
│   │   ├── registry.py      # Tool registry loading
│   │   ├── browser.py       # Warmup (5s wait + timeout)
│   │   ├── telegram.py      # Async handshake
│   │   └── __init__.py      # Pipeline assembly
│   ├── browser_daemon.py    # Browser driver management
│   ├── browser_lock.py      # Lock for restore operations
│   ├── logger/              # Dual logging system
│   └── *.py                 # security, helpers, etc.
├── tests/                   # Unit tests
│   ├── test_backup.py       # Schema, validation, Pydantic compat
│   └── test_browser_e2e.py  # Browser automation
├── app.py                   # FastAPI entrypoint (refactored)
├── config.py                # API key and global configuration
└── requirements.txt         # Dependencies
```

### Non-Obvious Structures
- **`deprecated/`** - 70% repository volume, imports disabled, never executed. Contains legacy tools (finance, research, polymarket, quiz) and old bot architecture.
- **`tests/`** - Unit tests for backup system and browser E2E only
- **No automatic migration**: Manual schema changes via reconciler only
- **`tools/scraper/prompts.py`** - Canonical prompt module after PLAN-02 (replaced `prompt.py`)

---

## 4. Core Concepts & Domain Model

### Key Abstractions

**1. Master Tables (Protected)**
- `scraped_articles` - Content with `vec_rowid` reference
- `scraped_articles_vec` - Vector embeddings (vec0 virtual table)
- `long_term_memories` - Persistent agent memory
- `long_term_memories_vec` - Memory embeddings
- **Excluded**: `scraped_articles_fts` (derived, rebuilt post-restore)

**2. Single-Writer Queue**
- `enqueue_write(sql, params)` - Single statement
- `enqueue_transaction(statements)` - Batched transaction (restore: batch_size=500)
- `wait_for_writes(timeout)` - Synchronous barrier

**3. Watermark-Based Delta**
- **Table-watermarks**: Per-table ISO-8601 `last_export_ts` in `watermark.json`
- **Delta selection**: `WHERE updated_at > ?` (parameterized)
- **Exclusive writes**: Never modify existing Parquet files
- **ISO-8601 only**: All delta comparisons use `.isoformat()`

**4. ULID Identification**
- Job IDs, Article IDs, Batch IDs (8-byte truncation for SQLite integer)
- **Critical**: `id TEXT PRIMARY KEY` in scraped_articles

**5. UPSERT Semantics**
- **Old (broken)**: `INSERT OR REPLACE` → destroys `id`, rotates `vec_rowid`
- **DO NOT USE**: `INSERT OR REPLACE`
- **Correct**: `INSERT ... ON CONFLICT(normalized_url) DO UPDATE` → preserves `vec_rowid`

**6. FTS5 External Content**
- **Excluded from backup**: Not in `MASTER_TABLES`
- **Rebuilt post-restore**: `INSERT INTO scraped_articles_fts(scraped_articles_fts) VALUES('rebuild')`
- **Synchronous**: Blocks via `wait_for_writes(timeout=300.0)`

**7. FTS5 Categorization (PLAN-02 Fix)**
- **`ALL_FTS_TABLES`**: Dict in `schemas/__init__.py`
- **Reconciler exclusion**: FTS tables skipped from standard drift detection
- **Separate handling**: Created via dedicated loop with simple existence check

### Schema Evolution Evidence

**Current Master Table Definition** (`database/schemas/__init__.py`):
```python
# RULE: MASTER_TABLES must be ordered list (parents before children) for FK-safe restores.
# RULE: Derived/External FTS tables (e.g., scraped_articles_fts) must NEVER be included here.
MASTER_TABLES: list[str] = [
    "scraped_articles",
    "scraped_articles_vec",
    "long_term_memories",
    "long_term_memories_vec",
]

ALL_FTS_TABLES: Dict[str, str] = {
    **vector.FTS_TABLES,
}
```

**Virtual Table Reconciliation** (`database/schema_introspector.py`):
```python
# For virtual tables (FTS5 / vec0), use existence-based reconciliation
if is_virtual:
    return table_exists(conn, table_name)
```

**Prompt Module Migration** (`tools/scraper/prompts.py`):
```python
"""
Prompts for the Scraper tool (AnythingTools adaptation).
All prompts MUST require the LLM to return strict JSON.
"""
SCRAPER_SYS_PROMPT = (
    "You are the Scraper sub-agent running in the 'scraper' agent_domain. "
    "All outputs MUST be valid JSON. ...\n"
)
```

---

## 5. Detailed Behavior

### 5.1 Export Streaming & Parameterization

**Chunked Export** (`tools/backup/exporter.py`):
```python
export_table_chunks(conn, table_name, config, mode="full", last_ts=""):
    # FTS tables are exported only via explicit error
    if table_name.endswith("_fts"):
        raise ValueError(f"FTS tables must not be exported directly: {table_name}")
    
    params = ()
    if mode == "delta" and last_ts:
        query += " WHERE updated_at > ?"
        params = (last_ts,)
    
    chunk_iter = pd.read_sql_query(query, conn, chunksize=config.batch_size, params=params)
```

**Batch Size Enforcement** (`tools/backup/config.py`):
```python
batch_size = min(getattr(global_config, "BACKUP_BATCH_SIZE", 500), 10000)
# OOM ceiling: 10,000 rows max
```

### 5.2 Embedding Validation & Atomic Storage

**DataFrame Validation** (`tools/backup/storage.py`):
```python
def _validate_embedding_column(df: pd.DataFrame, table_name: str):
    if "embedding" not in df.columns:
        return
    embeddings = df["embedding"].dropna()
    for idx, emb in embeddings.items():
        if isinstance(emb, bytes):
            validate_embedding_bytes(emb)  # Raises ValueError on mismatch
```

**Atomic Write** (`tools/backup/storage.py`):
```python
dest = config.table_dir(table_name) / f"{table_name}_{ts}.parquet"
temp_path = dest.with_suffix(".tmp.parquet")
writer = pq.ParquetWriter(str(temp_path), schema, compression=config.compression)
# Write batches...
writer.close()
temp_path.replace(dest)  # Atomic rename
```

### 5.3 Single-Writer Restore & FTS Rebuild

**Restore with Transaction Batching** (`tools/backup/restore.py`):
```python
for batch in parquet_file.iter_batches(batch_size=500):
    pylist = batch.to_pylist()
    statements = []
    for row in pylist:
        params = [row.get(col_name) for col_name in matched_cols]
        statements.append((sql, tuple(params)))
    
    enqueue_transaction(statements)
    count += len(statements)

# Synchronously wait for background writer
asyncio.run(wait_for_writes(timeout=120.0))
```

**FTS Rebuild Synchronous** (`tools/backup/restore.py`):
```python
if restored_counts.get("scraped_articles", 0) > 0:
    enqueue_write("INSERT INTO scraped_articles_fts(scraped_articles_fts) VALUES('rebuild')")
    asyncio.run(wait_for_writes(timeout=300.0))  # Blocks until complete
```

### 5.4 Read-Only Runner Connection

**Export Only Reads** (`tools/backup/runner.py`):
```python
def run(mode: str = "delta", ...):
    conn = DatabaseManager.get_read_connection()
    try:
        result = export_all_tables(conn, config, mode=mode)
    finally:
        pass  # Keep thread-local connection alive
```

### 5.5 Scraper Pipeline

**Tool Execution** (`tools/scraper/tool.py`):
```python
from tools.scraper.prompts import SCRAPER_SYS_PROMPT, CURATION_SYS_PROMPT
# Uses canonical constants from prompts.py (post-PLAN-02)
```

### 5.6 Browser Warmup (With Timeout)

**Warmup Sequence** (`utils/startup/browser.py`):
```python
def _do_warmup():
    browser_lock.acquire()
    try:
        driver = get_or_create_driver()
        driver.short_random_sleep(5.0)  # Wait 5s before navigation
        driver.get("https://example.com")
        html = driver.page_html or ""
        if "Example Domain" not in html:
            raise RuntimeError("Verification failed")
    finally:
        browser_lock.safe_release()

result = await asyncio.wait_for(asyncio.to_thread(_do_warmup), timeout=35.0)
```

---

## 6. Public Interfaces

### API Endpoints

**Tool Enqueueing**:
```bash
curl -X POST http://localhost:8000/api/tools/scraper \
  -H "X-API-Key: dev_default_key" \
  -d '{"args": {"url": "https://example.com"}}'
# Returns: {"status": "QUEUED", "job_id": "01H7Y..."}
```

**Backup Export**:
```bash
curl -X POST http://localhost:8000/api/backup/export?mode=full \
  -H "X-API-Key: dev_default_key"
# Returns: {"status": "EXPORT_QUEUED", "job_id": "01H7Y..."}
```

**Backup Restore**:
```bash
curl -X POST http://localhost:8000/api/backup/restore \
  -H "X-API-Key: dev_default_key"
# Returns: {"status": "RESTORE_QUEUED", "job_id": "01H7Z..."}
```

**Job Status**:
```bash
curl http://localhost:8000/api/jobs/{job_id} \
  -H "X-API-Key: dev_default_key"
# Returns: {status, logs, final_payload}
```

### Python/CLI

**Manual Full Backup**:
```python
from database.connection import DatabaseManager
from tools.backup.storage import export_all_tables

conn = DatabaseManager.get_read_connection()
export_all_tables(conn, mode="full")
conn.close()
```

**Manual Restore**:
```python
from database.connection import DatabaseManager
from tools.backup.restore import restore_master_tables_direct

conn = DatabaseManager.get_read_connection()
restore_master_tables_direct(conn)
conn.close()
```

---

## 7. State, Persistence, and Data

### Database Schema (v9)

**Master Tables**:
```sql
CREATE TABLE scraped_articles (
    id TEXT PRIMARY KEY,           -- ULID
    vec_rowid INTEGER NOT NULL,    -- References vec0 rowid
    normalized_url TEXT UNIQUE,    
    url TEXT NOT NULL,
    title TEXT, conclusion TEXT, summary TEXT,
    metadata_json TEXT DEFAULT '{}',
    embedding_status TEXT CHECK(...),
    scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP  -- Required for delta
);
```

**Triggers**: FTS5 sync + `updated_at` auto-update

### Backup Data Format

```
backups/
  scraped_articles/
    scraped_articles_20260426_055301234567.parquet
  scraped_articles_vec/
    scraped_articles_vec_20260426_055301234567.parquet
  watermark.json
```

**Watermark** (ISO-8601):
```json
{
  "table_watermarks": {
    "scraped_articles": "2026-04-26T11:05:22.600Z",
    "scraped_articles_vec": "2026-04-26T11:05:22.600Z"
  }
}
```

---

## 8. Dependencies & Integration

### Runtime Dependencies
- `pyarrow>=15.0.0` - Parquet I/O (mandatory)
- `pandas>=2.0.0` - Chunked DataFrames
- `sqlite-vec>=0.1.0` - Vector extension (optional)
- `fastapi`, `uvicorn` - API
- `botasaurus` - Browser automation
- `python-telegram-bot` - Telegram delivery
- `psutil==5.9.5` - Process cleanup

### Environment Variables
```bash
API_KEY="dev_default_key"
BACKUP_ENABLED=true
BACKUP_BATCH_SIZE=500      # Enforced ceiling 10000 in code
BACKUP_COMPRESSION="zstd"
ANYTHINGLLM_ARTIFACTS_DIR="/path/to/artifacts"
CHROME_USER_DATA_DIR="chrome_profile"
TELEGRAM_BOT_TOKEN="token"
```

### Integration Points
- **Scraper → Backup**: Post-persistence delta trigger
- **Reconciler → Backup**: Pre-drop snapshot trigger
- **Restore → FTS**: Synchronous rebuild after data
- **Writer Queue**: All writes to single thread (`writer.py`)
- **Prompts → Scraper**: Uses canonical module post-PLAN-02

### Tight Coupling
- `MASTER_TABLES` ordered list ↔ FK constraints
- `updated_at` column ↔ Delta backup logic
- `VECTOR_BYTE_LENGTH` ↔ 1024 float32s (fixed)
- `batch_size=500` ↔ OOM safety
- `prompts.py` constants ↔ Scraper tool execution

---

## 9. Setup, Build, and Execution

### Clean Setup
```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Verify pyarrow
python -c "import pyarrow; assert pyarrow.__version__ >= '15.0.0'"

# 3. Environment
cp .env.example .env
# Ensure BACKUP_ENABLED=true, BACKUP_BATCH_SIZE=500

# 4. Start API
python -m uvicorn app:app --reload --port 8000
```

### Manual Operations

**Full Backup**:
```bash
python -c "
from database.connection import DatabaseManager
from tools.backup.storage import export_all_tables
conn = DatabaseManager.get_read_connection()
export_all_tables(conn, mode='full')
conn.close()
"
```

**Restore**:
```bash
python -c "
from database.connection import DatabaseManager
from tools.backup.restore import restore_master_tables_direct
conn = DatabaseManager.get_read_connection()
restore_master_tables_direct(conn)
conn.close()
"
```

---

## 10. Testing & Validation

### Unit Tests (`tests/`)
- **test_backup.py**: Schema validity, embedding validation, Pydantic compat, Pandas round-trip
- **test_browser_e2e.py**: Browser automation end-to-end tests

### Manual Verification
```bash
# Run tests
pytest tests/test_backup.py -v

# Validate reconcile
python -c "
from database.reconciler import SchemaReconciler
from database.connection import DatabaseManager
conn = DatabaseManager.create_write_connection()
reconciler = SchemaReconciler(conn)
report = reconciler.reconcile()
print('Actions:', [a for a in report.actions if a.action != 'unchanged'])
"
```

### Gaps (No Coverage)
- **No integration test**: Full pipeline
- **No API test**: Backup endpoints
- **No delta diff test**: Parquet vs DB
- **No pyarrow version check**: Runtime

---

## 11. Known Limitations & Non-Goals

### Critical Constraints
1. **Single-Writer**: Restore blocks on active scraper via `browser_lock`
2. **All-or-Nothing**: No selective table restore
3. **Parquet Immutability**: Files never modified, only deleted
4. **No Verification**: No checksums
5. **Batch Size Ceiling**: 10,000 rows max (OOM prevention)

### Runtime Limits
- Worker: 1s polling
- Restore wait: 120s per table, 300s for FTS rebuild
- Writer queue: max 1000 enqueued tasks
- Timeout: No internal enforcement (jobs may hang)

### Architectural Trade-offs

**Pros**:
- **OOM safety**: Chunked 500 rows / streaming
- **Data safety**: Atomic writes, pre-drop snapshots, UPSERT preservation
- **Schema correctness**: Canonical DDL, explicit PyArrow schemas
- **Recoverable**: From drift, corruption, partial writes

**Cons**:
- **Performance**: 500-row chunks slow for bulk
- **Complexity**: 3-layer backup system + reconciler
- **Dependency**: pyarrow mandatory (heavy)
- **Sync FTS rebuild**: 300s potential blocking

### Explicit Non-Goals
- Continuous backup / real-time sync
- Partial restore / incremental restore
- Cloud sync (OneDrive optional)
- Multi-dataset (single DB)

---

## 12. Change Sensitivity

### Most Fragile Components

**1. `tools/backup/exporter.py`**
- **Risk**: Virtual table handling (must select `rowid`)
- **Risk**: Delta `last_ts` format (ISO-8601 required)
- **Fix Applied**: Parameterized queries, FTS exclusion

**2. `database/reconciler.py`**
- **Risk**: Pre-drop snapshot must use `export_table_chunks`
- **Risk**: FK cascade traversal
- **Fix Applied**: Correct export call, FTS table exclusion

**3. `tools/backup/restore.py`**
- **Risk**: Byte guard for `pd.isna()`
- **Risk**: Sync FTS rebuild timeout
- **Fix Applied**: `enqueue_transaction`, `wait_for_writes`

**4. `tools/backup/storage.py`**
- **Risk**: Iterator contract `(DataFrame, count)`
- **Risk**: Atomic rename (`.tmp` → final)
- **Fix Applied**: Embedding validation, ISO-8601 watermarks

**5. `database/schemas/__init__.py`**
- **Risk**: MASTER_TABLES must be ordered list, no FTS
- **Fix Applied**: Explicit rules, list type, `ALL_FTS_TABLES`

**6. `database/schema_introspector.py`**
- **Risk**: Virtual table DDL comparison
- **Fix Applied**: Existence-based reconciliation for FTS5/vec0

**7. `api/schemas.py`**
- **Risk**: Watermark schema compatibility
- **Fix Applied**: `table_watermarks` field

**8. `tools/scraper/prompts.py`**
- **Risk**: Prompt constant naming, formatting errors
- **Fix Applied (PLAN-02)**: Canonical module, fixed typos, deleted legacy `prompt.py`

### Tight Coupling
- **Schema v9**: `updated_at` required for delta
- **PyArrow 15+**: Binary embeddings (variable length)
- **Batch size**: 500 enforced (10000 ceiling)
- **Browser lock**: Restore requires exclusive access
- **Prompt module**: Scraper relies on `prompts.py` constants

### Easy Extension
- **Add tables**: Update `MASTER_TABLES`, `TABLE_SCHEMAS`
- **Change compression**: `BACKUP_COMPRESSION`
- **Tune memory**: Decrease batch size
- **Add OneDrive**: Set `BACKUP_ONEDRIVE_DIR`

### Hardest Refactors
- Remove pyarrow: Rewrite all backup I/O
- Async backup: `aiofiles` + async DB
- Schema v10: Update DDL + migration
- API update: Wire new endpoints
- Prompt migration: Rename module, update imports