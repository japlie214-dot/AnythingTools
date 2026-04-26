# AnythingTools - Deterministic Tool Hosting Service

## 1. Project Overview

AnythingTools is a deterministic tool-hosting service exposing web scraping, publishing, batch reading, and backup tools via FastAPI. It enforces thread-based tool execution, single-writer database architecture (SQLite WAL), and structured markdown callbacks.

### Operational Capabilities
- **Web Scraper**: Strict DOM validation, video/audio rejection, ULID-based identification, automatic delta backup post-persistence
- **Publisher**: Telegram delivery with state management and crash recovery
- **Batch Reader**: Semantic search over scraped content (vector + full-text)
- **Backup System**: OOM-safe Parquet export/import for 5 master tables with intelligent restoration, streaming chunks, and atomic writes

### Explicit Non-Capabilities
- **No continuous backup**: Batch-only execution (triggered or manual)
- **No selective restore**: All-or-nothing restoration for master tables
- **No telemetry**: Local SQLite only
- **No real-time streaming**: Polling-based worker architecture (1s interval)
- **No automatic migration**: Manual schema reconciliation only
- **No concurrent writers**: Single background writer thread
- **No backup verification**: No checksums or corruption detection
- **No direct FTS backup**: FTS tables excluded from restores, rebuilt post-restore

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
  - Master: `scraped_articles`, `scraped_articles_vec`, `long_term_memories`, `long_term_memories_vec`
  - Non-master: `jobs`, `job_items`, `job_logs`, `broadcast_batches`
- **Schema Reconciliation** (`reconciler.py`): Detects drift, performs pre-drop snapshots, cascades FK recreations

**4. Tool Implementations (`tools/`)**
- **Scraper**: Full pipeline (extraction → curation → persistence → backup) with job_items tracking
- **Publisher**: Telegram delivery, state management via `job_items`
- **Batch Reader**: Hybrid vector + FTS5 search
- **Backup**: Multi-table Parquet export/import with streaming

**5. Backup System (`tools/backup/`)**
- **Config** (`config.py`): OOM-safe batch size clamping (max 10000)
- **Schema** (`schema.py`): PyArrow schemas with binary embeddings, embedding validation
- **Exporter** (`exporter.py`): Chunked SQL reads (500 rows/batch), parameterized queries, virtual table aware
- **Storage** (`storage.py`): Atomic Parquet writes, embedding validation, ISO-8601 watermarks
- **Restore** (`restore.py`): Single-writer queue routing (batch 500), adaptive column mapping, synchronous FTS rebuild
- **Runner** (`runner.py`): Orchestrates backup/restore with job tracking, read-only connections
- **Models** (`models.py`): Pydantic v1/v2 compatibility for serialization

### Execution Model
- **API**: Event-driven (FastAPI)
- **Worker**: Polling-based (1s interval)
- **Tools**: Thread-isolated execution
- **Database**: Single-writer, multi-reader (WAL)
- **Backup**: Streaming chunked execution (prevent OOM)

### Data Flow (Backup/Restore)

**Backup Export (Full/Delta)**:
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

**Restore with FTS Rebuild**:
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

### Top-Level Directories
```
./
├── api/                    # FastAPI routes + schemas (updated: table_watermarks)
├── bot/                    # Worker engine  
├── clients/                # External services (LLM, Snowflake)
├── database/               # SQLite layer
│   ├── schemas/            # Canonical DDL (single source of truth)
│   │   ├── __init__.py     # MASTER_TABLES: list[str] (no FTS)
│   │   ├── vector.py       # 5 master table DDL + FTS5 triggers
│   │   └── *.py            # jobs, finance, pdf, token
│   ├── reconciler.py       # Schema drift detection + repair
│   ├── schema_introspector.py  # PRAGMA parsing + DDL comparison
│   ├── lifecycle.py        # Uses reconciler, removes versioning
│   ├── writer.py           # Background single-writer thread + enqueue_transaction
│   ├── connection.py       # DB connection manager (optional vec0, query_only)
│   └── health.py           # Table validation
├── deprecated/             # Legacy code (~70% volume, never loaded)
├── tools/                  # Tool implementations
│   ├── scraper/            # Extraction, curation, persistence + auto-backup
│   ├── publisher/          # Telegram delivery
│   ├── batch_reader/       # Semantic search
│   ├── backup/             # Hardened backup system (Phase 3)
│   │   ├── __init__.py
│   │   ├── config.py       # Batch size ceiling, rule comments
│   │   ├── models.py       # Watermark/Result with table_watermarks + model_dump_compat()
│   │   ├── schema.py       # PyArrow schemas (binary embeddings, validation helper)
│   │   ├── exporter.py     # Parameterized queries, FTS exclusion
│   │   ├── storage.py      # Atomic writes + embedding validation
│   │   ├── restore.py      # enqueue_transaction + sync FTS rebuild
│   │   └── runner.py       # Read-only connection, ISO-8601 timestamps
└── utils/                  # Infrastructure
└── tests/                  # Unit tests (newly added)
    ├── test_backup.py      # Schema, validation, Pydantic compat tests
    └── test_migration_pipeline.py  # Placeholder replaced
```

### Critical Architecture Files
- `app.py` - Lifespan startup/shutdown
- `bot/engine/worker.py` - Job execution lifecycle
- `database/writer.py` - Single-writer thread + `enqueue_transaction`
- `database/connection.py` - WAL + optional vec0 + `query_only` mode
- `database/schemas/__init__.py` - MASTER_TABLES definition (ordered list, no FTS)
- `tools/backup/runner.py` - Read-only exports, restore routing

### Non-Obvious Structures
- **`deprecated/`** - 70% repository volume, imports disabled, never executed
- **`tests/`** - Post-Phase 3: `test_backup.py` added, migration placeholder replaced
- **No automatic migration**: Manual schema changes only via reconciler

---

## 4. Core Concepts & Domain Model

### Key Abstractions

**1. Master Tables (Protected)**
- `scraped_articles` - Content storage with `vec_rowid` reference
- `scraped_articles_vec` - Vector embeddings (vec0 virtual table, binary column)
- `long_term_memories` - Persistent agent memory
- `long_term_memories_vec` - Memory embeddings
- **Excluded**: `scraped_articles_fts` (derived, rebuilt post-restore)

**2. Single-Writer Queue**
- `enqueue_write(sql, params)` - Single statement
- `enqueue_transaction(statements)` - Batched transaction (restore uses batch_size=500)
- `wait_for_writes(timeout)` - Synchronous barrier

**3. Watermark-Based Delta**
- **Table-watermarks**: Per-table ISO-8601 `last_export_ts` in `watermark.json`
- **Delta selection**: `WHERE updated_at > ?` (parameterized)
- **Exclusive writes**: Only append, never modify existing Parquet files
- **ISO-8601 only**: All delta comparisons use `.isoformat()`

**4. ULID Identification**
- Job IDs, Article IDs, Batch IDs
- 8-byte truncation for SQLite integer compatibility
- **Critical**: `id TEXT PRIMARY KEY` in scraped_articles

**5. UPSERT Semantics**
- **Old**: `INSERT OR REPLACE` → destroys `id`, rotates `vec_rowid` → vector bloat
- **DO NOT USE**: `INSERT OR REPLACE`
- **Correct**: `INSERT ... ON CONFLICT(normalized_url) DO UPDATE` → preserves `vec_rowid`

**6. FTS5 External Content**
- **Excluded from backup**: `scraped_articles_fts` not in `MASTER_TABLES`
- **Rebuilt post-restore**: `INSERT INTO scraped_articles_fts(scraped_articles_fts) VALUES('rebuild')`
- **Synchronous**: Blocks via `wait_for_writes(timeout=300.0)`

### Schema Evolution Evidence

**Current Master Table Definition** (`database/schemas/__init__.py`):
```python
# RULE: MASTER_TABLES must be an ordered list (parents before children) for FK-safe restores.
# RULE: Derived/External FTS tables (e.g., scraped_articles_fts) must NEVER be included here.
# They cannot be restored directly and must be rebuilt post-restoration.
MASTER_TABLES: list[str] = [
    "scraped_articles",
    "scraped_articles_vec",
    "long_term_memories",
    "long_term_memories_vec",
]
```

**Embedding Schema** (`tools/backup/schema.py`):
```python
VECTOR_BYTE_LENGTH = 4096  # 1024 float32s
FLOAT32_COUNT = 1024

SCRAPED_ARTICLES_VEC_SCHEMA = pa.schema([
    pa.field("rowid", pa.int64(), nullable=False),
    pa.field("embedding", pa.binary(VECTOR_BYTE_LENGTH), nullable=False),  # FIXED: was fixed_size_binary
])
```

**Validation Helper** (`tools/backup/schema.py`):
```python
def validate_embedding_bytes(embedding_bytes: bytes, expected_length: int = VECTOR_BYTE_LENGTH) -> None:
    """Validate that embedding bytes match the expected fixed size."""
    if embedding_bytes is None:
        raise ValueError("Embedding bytes cannot be None")
    actual = len(embedding_bytes)
    if actual != expected_length:
        raise ValueError(f"Embedding size mismatch: expected {expected_length}, got {actual}")
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
        # Parameterized WHERE clause (safe, table_name validated against MASTER_TABLES)
        cursor = conn.execute(f"PRAGMA table_info({table_name})")
        cols = [r[1] for r in cursor.fetchall()]
        if "updated_at" in cols:
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
def _validate_embedding_column(df: pd.DataFrame, table_name: str) -> None:
    """Validate embedding byte lengths for vector tables before Parquet write."""
    if "embedding" not in df.columns:
        return
    embeddings = df["embedding"].dropna()
    for idx, emb in embeddings.items():
        if isinstance(emb, bytes):
            validate_embedding_bytes(emb)  # Raises ValueError on mismatch
```

**Atomic Write + Batch Iterator** (`tools/backup/storage.py`):
```python
def write_table_batch(table_name: str, chunks_iter, config: BackupConfig) -> int:
    schema = TABLE_SCHEMAS[table_name]
    dest = config.table_dir(table_name) / f"{table_name}_{ts}.parquet"
    temp_path = dest.with_suffix(".tmp.parquet")
    
    writer = pq.ParquetWriter(str(temp_path), schema, compression=config.compression)
    for df, count in chunks_iter:
        if table_name.endswith("_vec"):
            _validate_embedding_column(df, table_name)
        table = pa.Table.from_pandas(df, schema=schema)
        writer.write_table(table)
    writer.close()
    temp_path.replace(dest)  # Atomic rename
```

### 5.3 Single-Writer Restore & FTS Rebuild

**Restore with Transaction Batching** (`tools/backup/restore.py`):
```python
from database.writer import enqueue_transaction, wait_for_writes
import asyncio

for batch in parquet_file.iter_batches(batch_size=500):
    pylist = batch.to_pylist()
    statements = []
    for row in pylist:
        params = [row.get(col_name) for col_name in matched_cols]
        statements.append((sql, tuple(params)))
    
    enqueue_transaction(statements)
    count += len(statements)

# Synchronously wait for background writer
try:
    loop = asyncio.get_running_loop()
    asyncio.run_coroutine_threadsafe(wait_for_writes(timeout=120.0), loop).result()
except RuntimeError:
    asyncio.run(wait_for_writes(timeout=120.0))
```

**FTS Rebuild Synchronous** (`tools/backup/restore.py`):
```python
if restored_counts.get("scraped_articles", 0) > 0:
    enqueue_write("INSERT INTO scraped_articles_fts(scraped_articles_fts) VALUES('rebuild')")
    # Blocks until rebuild completes
    try:
        loop = asyncio.get_running_loop()
        asyncio.run_coroutine_threadsafe(wait_for_writes(timeout=300.0), loop).result()
    except RuntimeError:
        asyncio.run(wait_for_writes(timeout=300.0))
```

### 5.4 Read-Only Runner Connection

**Export Only Reads** (`tools/backup/runner.py`):
```python
def run(mode: str = "delta", ...):
    # Exports only require read access. Do not close the thread-local read connection.
    conn = DatabaseManager.get_read_connection()
    try:
        result = export_all_tables(conn, config, mode=mode)
    finally:
        pass  # Keep thread-local connection alive
```

**Restore Reads Only** (`tools/backup/runner.py`):
```python
def restore(manual_job_id: Optional[str] = None):
    # Restore only requires read access to schema info;
    # writes are routed via enqueue_transaction.
    conn = DatabaseManager.get_read_connection()
    result = restore_master_tables_direct(conn)
```

---

## 6. Public Interfaces

### API Endpoints (Working After Phase 3)

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

**Status**:
```bash
curl http://localhost:8000/api/backup/status \
  -H "X-API-Key: dev_default_key"
# Returns: {enabled, backup_dir, watermark, file_counts, total_size_bytes}
```

**Job Tracking**:
```bash
curl http://localhost:8000/api/jobs/{job_id} \
  -H "X-API-Key: dev_default_key"
# Returns: {status, logs, final_payload}
```

### Python/CLI

**Manual Full Backup**:
```python
from tools.backup.storage import export_all_tables
from database.connection import DatabaseManager

conn = DatabaseManager.get_read_connection()
export_all_tables(conn, mode="full")
conn.close()  # Thread-local; safe to close
```

**Manual Restore**:
```python
from tools.backup.restore import restore_master_tables_direct
conn = DatabaseManager.get_read_connection()
restore_master_tables_direct(conn)
conn.close()
```

**Scraper (Auto-Backup)**:
```python
from tools.scraper.tool import ScraperTool
tool = ScraperTool()
# Auto-runs delta backup after persistence
```

---

## 7. State, Persistence, and Data

### Database Schema (v9, `updated_at` required)

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
    scraped_articles_20260426_055301234567.parquet  (newest only in full mode)
  scraped_articles_vec/
    scraped_articles_vec_20260426_055301234567.parquet
  ...
  watermark.json
```

**Watermark** (ISO-8601):
```json
{
  "table_watermarks": {
    "scraped_articles": "2026-04-26T11:05:22.600Z",
    "scraped_articles_vec": "2026-04-26T11:05:22.600Z",
    ...
  }
}
```

### PyArrow Schemas

- **Vector tables**: `pa.binary(4096)` (FIXED: was `fixed_size_binary`)
- **Validation**: `validate_embedding_bytes` raises on mismatch
- **FTS schema**: Excluded from `TABLE_SCHEMAS`

---

## 8. Dependencies & Integration

### Runtime Dependencies (from `requirements.txt`)
- `pyarrow>=15.0.0` - Parquet I/O (mandatory)
- `pandas>=2.0.0` - Chunked DataFrames
- `sqlite-vec>=0.1.0` - Vector extension (optional)
- `fastapi`, `uvicorn` - API
- `botasaurus` - Browser automation

### Environment Variables
```bash
BACKUP_ENABLED=true
BACKUP_ONEDRIVE_DIR=""
BACKUP_BATCH_SIZE=500      # Enforced ceiling 10000 in code
BACKUP_COMPRESSION="zstd"
API_KEY="dev_default_key"
```

### Integration Points
- **Scraper → Backup**: Post-persistence delta trigger
- **Reconciler → Backup**: Pre-drop snapshot trigger
- **Restore → FTS**: Synchronous rebuild after data
- **Writer Queue**: All writes to single thread (`writer.py`)

### Tight Coupling
- `MASTER_TABLES` ordered list ↔ FK constraints
- `updated_at` column ↔ Delta backup logic
- `VECTOR_BYTE_LENGTH` ↔ 1024 float32s (fixed)
- `batch_size=500` ↔ OOM safety

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

### New Unit Tests (`tests/test_backup.py`)
- **Schema validity**: Binary embedding fields
- **Embedding validation**: Correct sizes, error on mismatch
- **Pydantic compat**: `model_dump_compat()` results
- **Pandas round-trip**: Arrow → Parquet → Python types

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
- Backup verification with checksums
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
- **Fix Applied**: Correct export call

**3. `tools/backup/restore.py`**
- **Risk**: Byte guard for `pd.isna()`
- **Risk**: Sync FTS rebuild timeout
- **Fix Applied**: `enqueue_transaction`, `wait_for_writes`

**4. `tools/backup/storage.py`**
- **Risk**: Iterator contract (`(DataFrame, count)`)
- **Risk**: Atomic rename (`.tmp` → final)
- **Fix Applied**: Embedding validation, ISO-8601 watermarks

**5. `database/schemas/__init__.py`**
- **Risk**: MASTER_TABLES must be ordered list, no FTS
- **Fix Applied**: Explicit rules, list type

**6. `api/schemas.py`**
- **Risk**: Watermark schema compatibility
- **Fix Applied**: `table_watermarks` field

**7. `tools/backup/models.py`**
- **Risk**: Pydantic v1/v2 serialization
- **Fix Applied**: `model_dump_compat()`

### Tight Coupling
- **Schema v9**: `updated_at` required for delta
- **PyArrow 15+**: `binary(4096)` for embeddings
- **Batch size**: 500 enforced (10000 ceiling)
- **Browser lock**: Restore requires exclusive access

### Easy Extension
- Add tables: Update `MASTER_TABLES`, `TABLE_SCHEMAS`
- Change compression: `BACKUP_COMPRESSION`
- Tune memory: Decrease batch size
- Add OneDrive: Set `BACKUP_ONEDRIVE_DIR`

### Hardest Refactors
- Remove pyarrow: Rewrite all backup I/O
- Async backup: `aiofiles` + async DB
- Schema v10: Update DDL + migration
- API update: Wire new endpoints