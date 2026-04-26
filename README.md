# AnythingTools - Deterministic Tool Hosting Service

## 1. Project Overview

AnythingTools is a deterministic tool-hosting service exposing web scraping, publishing, batch reading, and backup tools via FastAPI. It enforces thread-based tool execution, single-writer database architecture (SQLite WAL), and structured markdown callbacks with retry mechanisms.

### Operational Capabilities
- **Web Scraper**: Strict DOM validation, video/audio rejection, ULID-based identification, automatic delta backup post-persistence
- **Publisher**: Telegram delivery with state management and crash recovery
- **Batch Reader**: Semantic search over scraped content (vector + full-text)
- **Backup System**: OOM-safe Parquet export/import for 5 master tables with intelligent restoration, pre-drop snapshots, and adaptive column mapping

### Explicit Non-Capabilities
- **No continuous backup**: Batch-only execution (triggered or manual)
- **No selective restore**: All-or-nothing restoration for master tables
- **No telemetry**: Local SQLite only
- **No real-time streaming**: Polling-based worker architecture (1s interval)
- **No automatic migration**: Manual schema reconciliation only
- **No concurrent writers**: Single background writer thread
- **No backup verification**: No checksums or corruption detection

---

## 2. High-Level Architecture

### Core Components

**1. API Layer (`app.py`, `api/`)**
- FastAPI with lifespan hook for startup/shutdown tasks
- Mounted `/artifacts` static file server
- Background task execution for export/restore operations
- **Endpoints**: 
  - `/api/tools/{tool}` - Tool job enqueueing
  - `/api/jobs/{id}` - Job status retrieval with logs
  - `/api/backup/export` - Manual backup trigger (queued, returns job_id)
  - `/api/backup/restore` - Manual restore trigger (queued, requires browser_lock)
  - `/api/backup/status` - Backup directory status
  - `/api/metrics` - System metrics (write queue, active jobs)

**2. Worker Manager (`bot/engine/worker.py`)**
- `UnifiedWorkerManager`: Polls database every 1s for `QUEUED` jobs
- Thread-isolated tool execution
- Callback delivery with exponential backoff (3 attempts)
- **Job lifecycle**: `QUEUED` → `RUNNING` → `COMPLETED|FAILED|PARTIAL|PENDING_CALLBACK|INTERRUPTED`
- **Recovery**: Handles `INTERRUPTED` jobs on restart

**3. Database Layer (`database/`)**
- **Single-writer background thread** (`writer.py`) with write queue (max 1000)
- **WAL mode** for concurrent readers
- **Schema v9** with `updated_at` tracking for delta backups
- **Current tables**: 
  - Master: `scraped_articles`, `scraped_articles_vec`, `scraped_articles_fts`, `long_term_memories`, `long_term_memories_vec`
  - Non-master: `jobs`, `job_items`, `job_logs`, `broadcast_batches`
- **Schema Reconciliation** (`reconciler.py`): Detects drift, performs pre-drop snapshots, cascades FK recreations

**4. Tool Implementations (`tools/`)**
- **Scraper**: Full pipeline (extraction → curation → persistence → backup) with job_items tracking
- **Publisher**: Telegram delivery, state management via `job_items`
- **Batch Reader**: Hybrid vector + FTS5 search
- **Backup**: Multi-table Parquet export/import with streaming

**5. Backup System (`tools/backup/`)**
- **Exporter** (`exporter.py`): Chunked SQL reads (500 rows/batch), virtual table handling
- **Storage** (`storage.py`): Atomic Parquet writes via temp-then-rename, watermark management
- **Restore** (`restore.py`): Adaptive column mapping, byte-array guard, context manager transactions
- **Runner** (`runner.py`): Orchestrates backup/restore with job tracking, browser_lock acquisition
- **Integration**: `SchemaReconciler` with pre-drop snapshots

### Execution Model
- **API**: Event-driven (FastAPI)
- **Worker**: Polling-based (1s interval)
- **Tools**: Thread-isolated execution
- **Database**: Single-writer, multi-reader (WAL)
- **Backup**: Streaming chunked execution (prevent OOM)

### Data Flow (Pre-Drop Snapshot + Restore)
```
[Schema Drift] → [SchemaReconciler]
    ↓
[Detect Master Table Recreation]
    ↓
[export_table_chunks(conn, table, mode="full")] → chunks of (DataFrame, count)
    ↓
[write_table_batch(name, chunks)] → writes Parquet file atomically
    ↓
[DROP TABLE] → [CREATE TABLE from canonical DDL]
    ↓
[restore_master_tables_direct(conn, [recreated tables])] 
    → Adaptive column mapping (skips missing, uses defaults)
    → INSERT via matched columns
    ↓
[rebuild FTS5 index]
    ↓
[export_all_tables(conn, mode="full")] → Purge old snapshots
```

---

## 3. Repository Structure

### Top-Level Directories
```
./
├── api/                    # FastAPI routes + schemas
├── bot/                    # Worker engine  
├── clients/                # External services (LLM, Snowflake)
├── database/               # SQLite layer
│   ├── schemas/            # Canonical DDL (single source of truth)
│   │   ├── __init__.py     # MASTER_TABLES, ALL_TABLES, ALL_TRIGGERS
│   │   ├── vector.py       # 5 master table DDL + FTS5 triggers
│   │   └── *.py            # jobs, finance, pdf, token
│   ├── reconciler.py       # Schema drift detection + repair (FIXED)
│   ├── schema_introspector.py  # PRAGMA parsing + DDL comparison
│   ├── lifecycle.py        # Updated: Uses reconciler, removes versioning
│   ├── writer.py           # Background single-writer thread
│   ├── connection.py       # DB connection manager (optional vec0)
│   └── health.py           # Table validation
├── deprecated/             # Legacy code (~70% volume, never loaded)
├── tools/                  # Tool implementations
│   ├── scraper/            # Extraction, curation, persistence (FIXED)
│   ├── publisher/          # Telegram delivery
│   ├── batch_reader/       # Semantic search
│   ├── backup/             # Updated: Multi-table streaming backup (ALL FIXED)
│   │   ├── __init__.py
│   │   ├── config.py       # Table-centric directory layout
│   │   ├── models.py       # Watermark/Result with table_watermarks
│   │   ├── schema.py       # PyArrow schemas for 5 tables
│   │   ├── exporter.py     # export_table_chunks() - virtual table aware (FIXED)
│   │   ├── storage.py      # write_table_batch() + export_all_tables() (FIXED)
│   │   ├── restore.py      # restore_master_tables_direct() + byte guard (FIXED)
│   │   └── runner.py       # BackupRunner with job tracking (FIXED)
└── utils/                  # Infrastructure
```

### Files Modified by Phase 3 Fixes

**New/Replaced**:
- `database/reconciler.py` - Complete rewrite with pre-drop snapshots
- `database/schema_introspector.py` - New component for introspection
- `tools/backup/exporter.py` - Virtual table handling, streaming
- `tools/backup/storage.py` - Iterator contract, table-centric orchestration
- `tools/backup/restore.py` - Adaptive mapping, byte guard, context transactions

**Modified**:
- `api/routes.py` - **FIXED**: Job tracking for backup endpoints
- `database/lifecycle.py` - Migrated to reconciler
- `tools/backup/runner.py` - **FIXED**: manual_job_id parameter, job tracking
- `tools/scraper/tool.py` - **FIXED**: Removed parent_job_id, auto-backup

**Critical Architecture Files**:
- `app.py` - Lifespan startup/shutdown
- `bot/engine/worker.py` - Job execution lifecycle
- `database/writer.py` - Single-writer thread
- `database/connection.py` - WAL + optional vec0

### Non-Obvious Structures

- **`deprecated/`** - 70% of repository volume, imports disabled, never executed
- **`database/migrations/`** - **DELETED** (removed with Phase 3)
- **`database/migrations_archive/`** - **DELETED** (removed with Phase 3)
- **`database/schema.py`** - **DELETED** (legacy module)
- **No automatic migration**: Manual schema changes only via reconciler

---

## 4. Core Concepts & Domain Model

### Key Abstractions

**1. Master Tables (Protected)**
- `scraped_articles` - Content storage with `vec_rowid` reference
- `scraped_articles_vec` - Vector embeddings (vec0 virtual table)
- `scraped_articles_fts` - Full-text search index (FTS5 virtual table)
- `long_term_memories` - Persistent agent memory
- `long_term_memories_vec` - Memory embeddings

**2. Non-Master Tables (Expendable)**
- `jobs` - Job queue with resumable state
- `job_items` - Step tracking (idempotent via JSON extraction)
- `job_logs` - Structured logging
- `broadcast_batches` - Publishing batches

**3. Watermark-Based Delta**
- **Table-watermarks**: Per-table `last_export_ts` in `watermark.json`
- **Delta selection**: `WHERE updated_at > last_ts`
- **Exclusive writes**: Only append, never modify existing Parquet files

**4. ULID Identification**
- Job IDs, Article IDs, Batch IDs
- 8-byte truncation for SQLite integer compatibility
- **Critical**: `id TEXT PRIMARY KEY` in scraped_articles

**5. UPSERT Semantics**
- **Old**: `INSERT OR REPLACE` → destroys `id`, rotates `vec_rowid` → vector bloat
- **DO NOT USE**: `INSERT OR REPLACE`
- **Correct**: `INSERT ... ON CONFLICT(normalized_url) DO UPDATE` → preserves `vec_rowid`

**6. Job Tracking**
- **Manual triggers**: API generates `job_id`, persists `QUEUED` → worker updates status
- **Auto triggers**: Parent tool manages `job_items`, no `jobs` table entry
- **BackupRunner**: Accepts `manual_job_id` for API, generates its own for auto

### Schema Evolution Evidence

**Current Schema** (`database/schemas/vector.py`):
```python
TABLES = {
    "scraped_articles": "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    "scraped_articles_fts": "CREATE VIRTUAL TABLE ... USING fts5(...)",
    "long_term_memories": "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
}
VEC_TABLES = {
    "scraped_articles_vec": "CREATE VIRTUAL TABLE ... USING vec0(embedding float[1024])",
    "long_term_memories_vec": "..."
}
TRIGGERS = {
    "scraped_articles_ai": "AFTER INSERT → FTS5 sync",
    "scraped_articles_ad": "AFTER DELETE → FTS5 sync", 
    "scraped_articles_au": "AFTER UPDATE → FTS5 sync"
}
```

**Database Connection** (`database/connection.py`):
```python
SQLITE_VEC_AVAILABLE: bool = True/False (graceful fallback)
```

---

## 5. Detailed Behavior

### 5.1 Schema Reconciliation (NEW)

**Trigger**: Database startup via `database/lifecycle.py`

**Process**:
1. **Introspection**: Parse current schema via `PRAGMA table_info`, `PRAGMA foreign_key_list`, `sqlite_master`
2. **Comparison**: Normalize type affinity (`VARCHAR` → `TEXT`, `float[1024]` → `fixed_size_binary(4096)`)
3. **Classification**:
   - `unchanged`: Exact match with canonical DDL
   - `altered`: Missing columns, add via `ALTER TABLE ADD COLUMN`
   - `recreated`: Type/constraint mismatch via `DROP + CREATE`
4. **Master Protection**: Pre-drop snapshot for all 5 master tables
5. **Cascade**: If parent recreated, children recreated (FK dependency)
6. **Trigger Restoration**: All FTS5 triggers re-created

**Evidence** (`database/reconciler.py`):
```python
# Pre-Drop Snapshot - FIXED BUG (was export_table, now export_table_chunks)
if is_master:
    chunks = export_table_chunks(self.conn, name, config, mode="full")
    write_table_batch(name, chunks, config)  # Expects iterator, not DataFrame
```

### 5.2 Backup Export (Streaming)

**Input**: `mode="full"` or `mode="delta"`, `table_name`, `last_ts`

**Output**: DataFrame chunks

**Process**:
```python
def export_table_chunks(conn, table_name, config, mode, last_ts):
    # BUG FIX 2: Explicit virtual table detection
    if table_name in ALL_VEC_TABLES:
        query = f"SELECT rowid, embedding FROM {table_name}"
    elif table_name.endswith("_fts"):
        query = f"SELECT rowid, * FROM {table_name}"
    else:
        query = f"SELECT * FROM {table_name}"
    
    # Delta filter
    if mode == "delta" and last_ts:
        query += f" WHERE updated_at > '{last_ts}'"
    
    # OOM prevention: 500 rows per chunk
    for chunk in pd.read_sql_query(query, conn, chunksize=500):
        yield chunk, len(chunk)
```

**Critical Properties**:
- `chunksize=500` enforces streaming
- `rowid` must be explicitly selected for FTS5/vec0
- Timestamps remain in SQLite format (`YYYY-MM-DD HH:MM:SS`) for lexicographical comparison

### 5.3 Backup Storage (Atomic)

**Input**: `(table_name, chunks_iterator)`

**Output**: Parquet file, updated watermark

**Process**:
```python
def write_table_batch(table_name, chunks_iter, config):
    schema = TABLE_SCHEMAS[table_name]  # Explicit schema
    dest = config.table_dir(table_name) / f"{table_name}_{timestamp}.parquet"
    temp = dest.with_suffix(".tmp.parquet")
    
    writer = pq.ParquetWriter(str(temp), schema, compression=config.compression)
    total = 0
    for df, count in chunks_iter:
        if count == 0: continue
        table = pa.Table.from_pandas(df, schema=schema)
        writer.write_table(table)
        total += count
    
    writer.close()
    temp.replace(dest)  # Atomic
    return total
```

**Full Backup Cleanup**:
```python
if mode == "full":
    # Keep only newest snapshot per table
    for table in TABLE_SCHEMAS:
        files = sorted(table_dir.glob(f"{table}_*.parquet"))
        for f in files[:-1]: f.unlink()
```

**Bug Fix Applied**: `write_table_batch` signature changed to accept iterator, not DataFrame (Phase 3 fix).

### 5.4 Intelligent Restoration

**Input**: `conn`, `table_names`

**Process**:
```python
# BUG FIX 3: Byte array guard for pd.isna()
for _, row in df.iterrows():
    params = []
    for col_name in matched_cols:
        val = row[col_name]
        # Guard against TypeError on bytes/memoryview
        if not isinstance(val, (bytes, memoryview, bytearray)) and pd.isna(val):
            val = None
        if isinstance(val, memoryview): val = bytes(val)
        params.append(val)
```

**Adaptive Mapping**:
1. **Validate PKs exist**: Skip if backup missing required keys
2. **Validate NOT NULL**: Skip if required non-defaulted column missing
3. **Match by name**: Only insert columns present in both backup and schema
4. **Allow defaults**: SQLite applies `DEFAULT` for omitted columns

### 5.5 Scraper Integration

**Modified Flow** (`tools/scraper/tool.py`):
1. **Early Lock Release**: `browser_lock.safe_release()` → immediately after scraping
2. **UPsert**: `INSERT ... ON CONFLICT(normalized_url) DO UPDATE` (preserves `vec_rowid`)
3. **Auto-Backup**: After persistence, calls `export_all_tables(conn, mode="delta")`
4. **Job Items**: State tracking for `curate`, `artifacts`, `backup`, `callback`

**Post-Persistence Backup**:
```python
# In scraper tool
from tools.backup.storage import export_all_tables
export_all_tables(conn, mode="delta")
```

### 5.6 API Endpoints (Working After Phase 3)

**✅ Functional**:
- `POST /backup/export` - Generates job_id, persists QUEUED, returns job_id
- `POST /backup/restore` - Generates job_id, checks browser_lock, returns job_id
- `GET /backup/status` - Reports current watermark and file counts
- `GET /jobs/{job_id}` - Returns status, logs, final_payload with artifact URLs

**All endpoints now properly track jobs**:
```python
# API route
job_id = ULID.generate()
created = now_iso()
enqueue_write(
    "INSERT INTO jobs (job_id, session_id, tool_name, args_json, status, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)",
    (job_id, "0", "backup", json.dumps({"mode": mode}), "QUEUED", created, created)
)
background_tasks.add_task(BackupRunner.run, mode=mode, trigger_type="manual", manual_job_id=job_id)
return ExportQueuedResponse(status="EXPORT_QUEUED", message=f"{mode.capitalize()} export started", job_id=job_id)

# BackupRunner.run
if trigger_type == "manual":
    if manual_job_id:
        # Update existing job to RUNNING
        enqueue_write("UPDATE jobs SET status = 'RUNNING' WHERE job_id = ?", (manual_job_id,))
```

---

## 6. Public Interfaces

### Currently Available (Working)

**API Endpoints**:
```bash
# Manual backup (returns job_id for tracking)
curl -X POST http://localhost:8000/api/backup/export?mode=full \
  -H "X-API-Key: dev_default_key_change_me_in_production"

# Manual restore (returns job_id, requires browser_lock)
curl -X POST http://localhost:8000/api/backup/restore \
  -H "X-API-Key: dev_default_key_change_me_in_production"

# Check backup status
curl http://localhost:8000/api/backup/status \
  -H "X-API-Key: dev_default_key_change_me_in_production"

# Check job status
curl http://localhost:8000/api/jobs/{job_id} \
  -H "X-API-Key: dev_default_key_change_me_in_production"
```

**Python/CLI**:
```python
# Manual backup
from tools.backup.storage import export_all_tables
from database.connection import DatabaseManager

conn = DatabaseManager.create_write_connection()
conn.close()

# Manual restore
from tools.backup.restore import restore_master_tables_direct
conn = DatabaseManager.create_write_connection()
restore_master_tables_direct(conn)  # All master tables
conn.close()

# Scraper (auto-backup)
from tools.scraper.tool import ScraperTool
tool = ScraperTool()
# Auto-backup runs after persistence
```

### Scraper Tool Interface

**Input**: `{"target_site": "example.com", "max_articles": 10}`

**Behavior**: **Modified** - auto-backup post-persistence, no `parent_job_id`

**Output**: Structured callback with artifacts and backup status

---

## 7. State, Persistence, and Data

### Database Schema (v9)

**Master Tables**:
```sql
CREATE TABLE scraped_articles (
    id TEXT PRIMARY KEY,           -- ULID
    vec_rowid INTEGER NOT NULL,    -- References vec0 rowid
    normalized_url TEXT UNIQUE,    -- Upsert key
    url TEXT NOT NULL,
    title TEXT, conclusion TEXT, summary TEXT,
    metadata_json TEXT DEFAULT '{}',
    embedding_status TEXT CHECK(...),
    scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP  -- v9 column
);
```

**Triggers** (v9):
```sql
-- Auto-maintain updated_at on UPDATE
CREATE TRIGGER scraped_articles_updated_at_trigger AFTER UPDATE
BEGIN
    UPDATE scraped_articles SET updated_at = CURRENT_TIMESTAMP
    WHERE id = NEW.id AND OLD.updated_at = NEW.updated_at;
END;

-- FTS5 sync triggers (3 total)
CREATE TRIGGER scraped_articles_ai AFTER INSERT ...
```

### Backup Data Format

**Atomic Write Pattern**:
```
backups/
  scraped_articles/
    scraped_articles_20260426_055301234567.parquet.tmp
    scraped_articles_20260426_055301234567.parquet  (atomic rename)
    scraped_articles_20260426_055401234567.parquet  (newest only in full mode)
  scraped_articles_vec/
    scraped_articles_vec_20260426_055301234567.parquet
  ...
  watermark.json
```

**Watermark**:
```json
{
  "last_article_id": "01H7Y...",
  "last_export_ts": "2026-04-26 05:53:01",
  "total_articles_exported": 1500,
  "total_vectors_exported": 3000,
  "table_watermarks": {
    "scraped_articles": "2026-04-26 05:53:01",
    "scraped_articles_vec": "2026-04-26 05:53:01",
    ...
  }
}
```

### PyArrow Schemas (Critical)

**Table Scopes**:
- `scraped_articles`: All fields except `embedding`
- `scraped_articles_vec`: `rowid`, `embedding fixed_size_binary(4096)`
- `scraped_articles_fts`: `rowid`, `title`, `conclusion`, `summary`
- `long_term_memories`: All fields
- `long_term_memories_vec`: `rowid`, `embedding fixed_size_binary(4096)`

**Bug Fix Evidence**: The schema explicitly defines `fixed_size_binary(4096)` for embedding fields.

---

## 8. Dependencies & Integration

### Runtime Dependencies

**Required** (from `requirements.txt`):
- `pyarrow>=15.0.0` - Parquet I/O (critical, must be installed)
- `pandas>=2.0.0` - Chunked DataFrames
- `sqlite-vec>=0.1.0` - Vector extension (optional, graceful fallback)
- `fastapi`, `uvicorn` - API
- `botasaurus` - Browser automation

**New Code Dependencies**:
- `recon` - introspection patterns
- `dataclasses`, `typing` - type safety

### Environment Variables

```bash
# Backup Configuration
BACKUP_ENABLED=true          # Master switch
BACKUP_ONEDRIVE_DIR=""       # Optional fallback path
BACKUP_BATCH_SIZE=500        # Chunk size (updated from 1000)
BACKUP_COMPRESSION="zstd"    # Parquet compression

# Database (existing)
DB_PATH=./database.db

# API Security
API_KEY="dev_default_key_change_me_in_production"
```

**Note**: `BACKUP_BATCH_SIZE=500` enforces streaming (was 1000 in old README).

### Integration Points

1. **Scraper → Reconciler**: Startup healing (embedding_status='PENDING' → generate)
2. **Scraper → Backup**: Post-persistence delta export
3. **Reconciler → Backup**: Pre-drop snapshot trigger
4. **Backup → Restore**: Adaptive column mapping uses reconciler schemas

**Tight Coupling**:
- Canonical DDL in `database/schemas/` defines all operations
- PyArrow schemas must match canonical DDL exactly
- `updated_at` column exists for delta logic
- `vec_rowid` preservation critical (no `INSERT OR REPLACE`)

---

## 9. Setup, Build, and Execution

### Clean Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt  # Includes pyarrow>=15.0.0

# 2. Verify critical packages
python -c "import pyarrow; print(pyarrow.__version__)"

# 3. Environment
cp .env.example .env
# Ensure:
BACKUP_ENABLED=true
BACKUP_BATCH_SIZE=500

# 4. Start API
python -m uvicorn app:app --reload --port 8000
```

### Manual Operations

**Full Backup**:
```bash
python -c "
from database.connection import DatabaseManager
from tools.backup.storage import export_all_tables
conn = DatabaseManager.create_write_connection()
export_all_tables(conn, mode='full')
conn.close()
"
```

**Delta Backup** (same as above, `mode='delta'`)

**Restore**:
```bash
# Via API
curl -X POST http://localhost:8000/api/backup/restore \
  -H "X-API-Key: dev_default_key_change_me_in_production"

# Manual:
python -c "
from database.connection import DatabaseManager
from tools.backup.restore import restore_master_tables_direct
conn = DatabaseManager.create_write_connection()
restore_master_tables_direct(conn)
conn.close()
"
```

### Recovery from Legacy

**Migration sequence** (manual):
1. **Stop legacy system**, backup `database.db`
2. **Install pyarrow**
3. **Run full backup to convert format**:
   ```python
   from tools.backup.storage import export_all_tables
   from database.connection import DatabaseManager
   conn = DatabaseManager.create_write_connection()
   export_all_tables(conn, mode="full")  # Creates new Parquet format
   conn.close()
   ```
4. **Delete old `backup/` directory contents** (if exists)
5. **Restart API** - new reconciler will validate schema

---

## 10. Testing & Validation

### Manual Verification (Current State)

**1. PyArrow Available**:
```bash
# Expected: 15.0.0 or higher
python -c "import pyarrow; print(pyarrow.__version__)"
```

**2. Schema Reconciliation**:
```bash
python -c "
from database.reconciler import SchemaReconciler
from database.connection import DatabaseManager
conn = DatabaseManager.create_write_connection()
reconciler = SchemaReconciler(conn)
report = reconciler.reconcile()
print([a for a in report.actions if a.action != 'unchanged'])
conn.close()
"
```

**3. Chunked Export**:
```bash
python -c "
from database.connection import DatabaseManager
from tools.backup.exporter import export_table_chunks
from tools.backup.config import BackupConfig
conn = DatabaseManager.get_read_connection()
config = BackupConfig.from_global_config()
chunks = list(export_table_chunks(conn, 'scraped_articles', config, 'full'))
print(f'Got {len(chunks)} chunks')
conn.close()
"
```

**4. Intelligent Restore**:
```bash
# 1. Delete existing scraped_articles table
sqlite3 database.db 'DROP TABLE scraped_articles;'
# 2. Run restore
python -c "
from database.connection import DatabaseManager
from tools.backup.restore import restore_master_tables_direct
conn = DatabaseManager.create_write_connection()
restore_master_tables_direct(conn)
conn.close()
"
# 3. Verify data
sqlite3 database.db 'SELECT COUNT(*) FROM scraped_articles;'
"
```

**5. Pre-Drop Snapshot**:
```bash
# 1. Edit database/schemas/vector.py to break scraped_articles schema
# 2. Start API (triggers reconciler)
# 3. Check backups/ directory for new snapshot
# 4. Verify old snapshots purged
```

### Gaps (No Coverage)

- **No unit tests** in `tests/` directory
- **No integration test** for full pipeline
- **No API test** (endpoints use new functions)
- **No pyarrow version validation** in code
- **No corruption detection** for Parquet files
- **No delta verification** (Parquet vs DB mismatch possible)

---

## 11. Known Limitations & Non-Goals

### Critical Constraints

**1. Single-Writer Lock**
- Restore requires `browser_lock` acquisition
- Blocks on active scraper
- **No queue**: Request waits or fails

**2. All-or-Nothing Restore**
- Cannot restore single table selectively
- **No incremental restore**: Full snapshots only

**3. Parquet Immutability**
- Files never modified, only created/deleted
- **No corruption repair**: Must delete and re-run

**4. No Backup Verification**
- No checksums
- No schema validation against Parquet content
- **Trust in write integrity only**

**5. Memory Constraints**
- Max 500 rows per chunk in `export_table_chunks`
- `write_table_batch` streams through PyArrow writer
- **Cannot change**: Reduces to 10,000+ rows would cause OOM

### Hard Runtime Limits

**Database**:
- SQLite WAL mode only
- **No concurrent writers**: Single background thread
- **No schema auto-migration**: Manual only

**Time**:
- Worker polls every 1s
- Callback retry: exponential backoff
- **No timeout enforcement**: Jobs may hang indefinitely

### Architectural Trade-offs

**Pros**:
- **OOM safety**: Chunking prevents memory exhaustion
- **Data safety**: Atomic writes, pre-drop snapshots, UPSERT preservation
- **Schema correctness**: Canonical DDL single source of truth
- **Recoverable**: From corruption, from drift, from partial writes

**Cons**:
- **Performance**: 500 row chunks slow for massive datasets
- **Complexity**: 3-layer backup system (exporter/storage/restore) + reconciler
- **API debt**: New system wired to old endpoints
- **Heavy dependency**: pyarrow required but infrastructure heavy

### Explicit Non-Goals

- **Continuous backup**: Batch only
- **Real-time sync**: No change data capture (CDC)
- **Cloud sync**: OneDrive optional, not mandatory
- **Partial restore**: All-or-nothing
- **No backup verification**: No checksums
- **Asynchronous backup**: Synchronous snapshots
- **Multi-dataset**: Single `database.db` + `backups/`

---

## 12. Change Sensitivity

### Most Fragile Components

**1. `tools/backup/exporter.py`**
- **Virtual table handling**: Must explicitly select `rowid`
- **Type inference**: `pd.read_sql_query` must not coerce binary columns
- **Delta logic**: `last_ts` format must match SQLite exactly
- **Bug Fix Applied**: Virtual table detection, removed `export_table()`

**2. `database/reconciler.py`**
- **Pre-drop snapshot**: Must use `export_table_chunks` (not `export_table`)
- **FK cascade**: Must traverse `pragma_foreign_key_list` correctly
- **Type normalization**: `VARCHAR` vs `TEXT` must be equivalent
- **Bug Fix Applied**: Correct export call, FTS5 handling

**3. `tools/backup/restore.py`**
- **Byte guard**: Must protect `pd.isna()` from bytes
- **Adaptive mapping**: Column names must match exactly
- **PK validation**: Cannot restore without required keys
- **Bug Fix Applied**: `isinstance` guard for byte arrays, context manager transactions

**4. `tools/backup/storage.py`**
- **Chunk iterator**: Yield `(DataFrame, count)` not DataFrame
- **Atomic writes**: `.tmp` → replace pattern critical
- **Watermark logic**: Per-table timestamps must be consistent
- **Bug Fix Applied**: Iterator contract, `export_all_tables()`

**5. `database/schemas/`**
- **Canonical DDL**: All reconciler decisions flow from here
- **Vector schemas**: Must match PyArrow specs exactly
- **Trigger definitions**: FTS5 sync must be complete

**6. `api/routes.py`**
- **Job tracking**: Must generate `job_id` before enqueueing
- **QUEUED persistence**: Must write to DB before background task
- **Parameter passing**: `manual_job_id` to BackupRunner
- **Bug Fix Applied**: All job tracking fixes

**7. `tools/scraper/tool.py`**
- **Job items**: Idempotent tracking for scraper pipeline
- **Backup call**: Must pass `trigger_type="auto"` without `parent_job_id`
- **Lock release**: Early release after scraping
- **Bug Fix Applied**: Removed `parent_job_id`, auto-backup works

### Tightly Coupled Areas

**Schema v9**:
- `updated_at` column required for delta
- **No fallback**: Missing column → full backup only

**PyArrow Schemas**:
- `fixed_size_binary(4096)` must match embedding size
- **No inference**: Explicit schema only
- **Version dependency**: >=15.0.0

**Batch Size**:
- `BACKUP_BATCH_SIZE=500` hard-enforced in code
- **Memory bound**: Cannot increase without OOM risk

**Browser Lock**:
- Restore acquires lock
- Blocks scraper
- **No deadlock prevention**: Manual coordination required

### Easy Extension Points

- **Add tables**: Update `TABLE_SCHEMAS`, `MASTER_TABLES`
- **New compression**: `BACKUP_COMPRESSION` (zstd, snappy, gzip)
- **OneDrive**: Change `BACKUP_ONEDRIVE_DIR`
- **Delta tuning**: `BACKUP_BATCH_SIZE` (decrease for memory, increase for speed)

### Hardest Refactors

- **Remove pyarrow**: Rewrite cache, exporter, storage
- **Async backup**: Rewrite I/O to `aiofiles` + async DB
- **Schema v10**: Update all schemas + migration
- **API update**: Wire new functions to FastAPI endpoints