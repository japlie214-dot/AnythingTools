# AnythingTools - Deterministic Tool Hosting Service

## 1. Project Overview

AnythingTools is a small, deterministic tool-hosting service that exposes a fixed set of tools via an HTTP API. It runs tools in threads, serializes all writes through a background writer to a SQLite database in WAL mode, and delivers structured markdown callbacks to an external service (AnythingLLM) with a durable retry mechanism.

The repository implements:
- **Web Scraper** with strict validation, DOM pre-checks, and video/audio rejection
- **Publisher** (Telegram) with automatic resumption and crash guards  
- **Batch Reader** for semantic search over scraped content
- **Parquet Delta Backup System** for incremental backup and restore of scraped articles and embeddings

**Key Changes (Current State):**
- **PLAN-01**: Scraper resilience (video rejection, DOM pre-checks, ULID validation)
- **PLAN-02**: Foundation for Parquet backups (pyarrow, schema updates, UPSERT refactoring)
- **PLAN-03**: Full backup pipeline with export, storage, restore, and API endpoints

**Critical Limitation**: The backup system requires `pyarrow>=15.0.0` but this is NOT yet installed in the environment. The code is present but non-functional until dependencies are satisfied.

---

## 2. High-Level Architecture

### Core Components

1. **API Layer** (`app.py`, `api/`)
   - FastAPI endpoints for job management and backup administration
   - Enhanced 422 validation with field-level error reporting
   - Background task execution for exports/restores

2. **Worker Manager** (`bot/engine/worker.py`)
   - `UnifiedWorkerManager` polls database and claims jobs
   - Thread-based tool execution for isolation
   - Automatic callback delivery with exponential backoff

3. **Database Layer** (`database/`)
   - SQLite with WAL mode for consistency
   - Single-writer background thread (`writer.py`)
   - Schema version v9 (with `updated_at` tracking)

4. **Tool Implementations** (`tools/`)
   - **Scraper**: End-to-end pipeline (extraction → curation → persistence → backup)
   - **Publisher**: Telegram delivery with state management
   - **Batch Reader**: Hybrid vector + full-text search
   - **Backup**: Parquet-based delta export/import

5. **Backup System** (`tools/backup/`)
   - **Exporter**: Cursor-paginated delta extraction
   - **Storage**: Atomic Parquet writes (temp-then-rename)
   - **Restore**: Deduplication and FTS5 rebuild

### Data Flow (Backup)

```
[SQLite DB] → [Exporter] → [Parquet Files] → [Storage]
   ↑                                      ↓
[Restore] ← [Watermark] ← [Atomic Writes]
```

### Execution Model

- **API**: Event-driven (FastAPI)
- **Worker**: Polling-based (1s interval)
- **Tools**: Thread-isolated execution
- **Backup**: Background tasks (export/restore)
- **Database**: Single-writer, multi-reader (WAL)

---

## 3. Repository Layout

### Top-Level Directories

- `api/` — FastAPI routes, schemas, backup endpoints
- `bot/` — Worker engine and job orchestration
- `clients/` — External services (LLM, Snowflake)
- `database/` — Connections, writer, migrations (v1-v9), schemas
- `deprecated/` — Legacy code (scraper research, finance tools)
- `tools/` — Tool implementations
  - `scraper/` — Extraction, curation, persistence, **backup trigger**
  - `publisher/` — Telegram delivery
  - `batch_reader/` — Semantic search
  - `draft_editor/` — Content editing
  - `backup/` — **NEW**: Parquet export/import (PLAN-02/03)
- `utils/` — Infrastructure (artifact manager, callback, logger)

### Critical New Files (Backup System)

```
tools/backup/
├── __init__.py          # Module documentation
├── config.py            # BackupConfig with OneDrive/local fallback
├── models.py            # Pydantic V1 models (Watermark, ExportResult, etc.)
├── schema.py            # Explicit PyArrow schemas (fixed_size_binary(4096))
├── exporter.py          # Cursor pagination, delta extraction
├── storage.py           # Atomic writes, watermark management
└── restore.py           # Deduplication, FTS5 rebuild

database/
├── schemas/vector.py    # updated_at column + trigger (v9)
└── migrations/
    └── v009_backup_updated_at.py  # Migration script

config.py                # BACKUP_* environment variables
requirements.txt         # pyarrow>=15.0.0 (pending install)

api/
├── schemas.py           # BackupResponse models
└── routes.py            # /backup/status, /backup/export, /backup/restore

tools/scraper/
├── tool.py              # Eearly browser_lock release + job_items
└── persistence.py       # UPSERT refactoring (preserves vec_rowid)
```

### Unconventional Structures

- **`deprecated/`** contains ~70% of repository volume but is never loaded
- **Migration v9** exists but is NOT automatically applied
- **Parquet schemas** are defined but environment lacks `pyarrow`
- **database/job_items** table used for resumable state tracking

---

## 4. Core Concepts & Domain Model

### Key Abstractions

**1. ULID-Based Identification**
- All IDs are ULIDs: job IDs, batch IDs, article IDs
- Article ID: 8-byte truncation for SQLite integer fit (`id INTEGER`)

**2. Updated-At Watermark**
- **New in v9**: `scraped_articles.updated_at` column
- `AFTER UPDATE` trigger auto-maintains timestamp
- Enables delta exports via lexicographical comparison

**3. Job Items (Resumable State)**
- `job_items` table tracks discrete steps: `curate`, `artifacts`, `backup`, `callback`
- Idempotent: `SELECT ... WHERE NOT EXISTS` prevents duplicates
- Resume-safe: Can re-run steps without restarting full pipeline

**4. Lock Management**
- `browser_lock`: Singleton threading.Lock for browser automation
- **Modified**: Scraper releases lock BEFORE heavy I/O (curation, persistence, backup)
- Restore requires lock acquisition (prevents concurrency issues)

**5. UPSERT Semantics**
- **Old**: `INSERT OR REPLACE` (destroys `id`, rotates `vec_rowid`)
- **New**: `INSERT ... ON CONFLICT(normalized_url) DO UPDATE`
- Preserves `vec_rowid` → prevents vector database bloat

### Data Models

**Watermark Schema** (`models.py`)
```python
class Watermark(BaseModel):
    last_article_id: str              # Last exported ULID
    last_export_ts: Optional[str]     # SQLite timestamp (NOT datetime)
    total_articles_exported: int
    total_vectors_exported: int
```

**Critical Design**: `last_export_ts` is `Optional[str]`, NOT `Optional[datetime]`. Why?
- SQLite: `YYYY-MM-DD HH:MM:SS`
- Python `.isoformat()`: `YYYY-MM-DDTHH:MM:SS`
- **Lexicographical comparison breaks**: `Space (32) < T (84)` → new articles appear "older"

**PyArrow Schemas** (`schema.py`)
```python
ARTICLES_SCHEMA = pa.schema([
    pa.field("id", pa.string(), nullable=False),
    pa.field("vec_rowid", pa.int64(), nullable=False),
    pa.field("embedding", pa.fixed_size_binary(4096), nullable=False),  # CRITICAL
    # ... other fields
])
```

---

## 5. Detailed Behavior

### 5.1 Scraper Pipeline (Modified)

1. **Job Creation**: `POST /api/tools/scraper`
2. **Browser Launch**: Headful browser via Botasaurus
3. **Scraping**: Link extraction, paywall detection, DOM pre-checks
4. **Early Lock Release**: `browser_lock.safe_release()` immediately after scraping
5. **Curation**: Top10Curator (80% budget, 3 retry)
6. **Persistence**: 
   - **OLD**: `INSERT OR REPLACE` → data loss
   - **NEW**: `INSERT ... ON CONFLICT DO UPDATE` → preserves `vec_rowid`
7. **Backup Trigger**: Automatic delta export via `export_delta()`
8. **Artifacts**: Manifest, raw JSON, curated JSON
9. **Callback**: Structured markdown with artifacts

### 5.2 Backup System (NEW)

#### Export (Delta)

```
Input: Watermark (last_article_id, last_export_ts)
Output: Parquet files + updated watermark
```

**Process**:
1. Read watermark from `watermark.json`
2. **Cursor pagination**: `WHERE updated_at > ? OR (updated_at = ? AND id > ?)`
3. Fetch batch (default 1000 rows)
4. Fetch vectors for `vec_rowid`s
5. Write Parquet (temp-then-rename)
6. Update watermark

**Atomicity**: 
- `.tmp.parquet` → `.parquet` (replace)
- `.tmp.json` → `.watermark.json` (replace)

#### Restore

```
Input: All Parquet files in backup/
Output: Reconstructed database (+ FTS5 rebuild)
```

**Process**:
1. Read all `articles_*.parquet` and `vectors_*.parquet`
2. Concatenate DataFrames
3. **Deduplicate**: `drop_duplicates(subset=["normalized_url"], keep="first")`
4. Queue writes via `enqueue_write()` (WAL-safe)
5. Rebuild FTS5 index

**Deduplication Strategy**: Last write wins (newest `updated_at`)

### 5.3 API Endpoints (Backup)

**New Endpoints**:
- `GET /backup/status` → `BackupStatusResponse`
- `POST /backup/export` → Background export (requires `BACKUP_ENABLED=true`)
- `POST /backup/restore` → Background restore (requires `browser_lock` free)

**Security**: Restore blocks if scraper is active to prevent lock contention.

### 5.4 State Tracking via Job Items

**Steps**:
1. `curate` → `top_10`, `target_count`
2. `artifacts` → `path` to `top10.json`
3. `backup` → `ExportResult` dict
4. `callback` → Final payload

**Resume Safety**: Each step checks `SELECT status FROM job_items WHERE step=?`, skips if `COMPLETED`.

### 5.5 Error Handling

**Backup Disabled**: If `BACKUP_ENABLED=false`, exporter returns `ExportResult(success=False, error="Disabled")`
**Missing PyArrow**: ImportError at runtime (code is present but dep missing)
**Lock Contention**: Restore refuses if `browser_lock.locked()`

---

## 6. Public Interfaces

### API Endpoints

**Existing**:
```bash
POST /api/tools/{tool_name}  # Enqueue job
GET  /api/jobs/{job_id}      # Status, logs, payload
DELETE /api/jobs/{job_id}    # CANCELLING signal
GET /api/manifest            # Tool registry
```

**New (Backup)**:
```bash
GET  /backup/status          # BackupStatusResponse
POST /backup/export          # Trigger delta export (background)
POST /backup/restore         # Trigger restore (background, requires lock)
```

### Tool Inputs (Scraper - Modified)

**No change to input shape**. However, internal behavior changed:
- **Behavior**: Runs backup automatically after persistence
- **State**: Uses `job_items` for resumable steps
- **Lock**: Releases early for concurrency

### Callback Payloads (Unchanged)

```json
{
  "_callback_format": "structured",
  "tool_name": "scraper",
  "status": "COMPLETED|PARTIAL|FAILED",
  "summary": "markdown",
  "details": { /* tool-specific */ },
  "artifacts": [ /* array */ ],
  "status_overrides": { /* optional */ }
}
```

### Backup Models

**ExportQueuedResponse**:
```json
{"status": "EXPORT_QUEUED", "message": "Delta export started in background"}
```

**BackupStatusResponse**:
```json
{
  "enabled": true,
  "backup_dir": "/path/to/backups",
  "watermark": {
    "last_article_id": "01H7Y...",
    "last_export_ts": "2026-04-25 12:00:00",
    "total_articles_exported": 1500,
    "total_vectors_exported": 1500
  },
  "article_files": 12,
  "vector_files": 12,
  "total_size_bytes": 52428800
}
```

---

## 7. State, Persistence, and Data

### Database Schema (v9 - Current)

**Tables**:
```sql
-- jobs, job_logs, broadcast_batches (existing)

-- scraped_articles (MODIFIED v9)
CREATE TABLE scraped_articles (
    id TEXT PRIMARY KEY,
    vec_rowid INTEGER NOT NULL,
    normalized_url TEXT UNIQUE,
    -- ... other fields ...
    scraped_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP  -- NEW
);

-- Trigger (v9)
CREATE TRIGGER scraped_articles_updated_at_trigger
AFTER UPDATE ON scraped_articles
BEGIN
    UPDATE scraped_articles SET updated_at = CURRENT_TIMESTAMP
    WHERE id = NEW.id AND OLD.updated_at = NEW.updated_at;
END;

-- scraped_articles_vec (existing, embedding BLOB)
```

**Migration v9** (`v009_backup_updated_at.py`):
```python
def up(conn, sqlite_vec_available):
    # Add column if missing
    # Backfill: updated_at = scraped_at
    # Create trigger
```

**Note**: Migration is NOT auto-applied. Must be manually executed.

### Backup Data Format

**Parquet Files**:
- `articles_{ts_from}_{ts_to}.parquet`
- `vectors_{ts_from}_{ts_to}.parquet`

**Watermark** (`watermark.json`):
```json
{
  "last_article_id": "01H7Y...",
  "last_export_ts": "2026-04-25 12:00:00",
  "total_articles_exported": 1500,
  "total_vectors_exported": 1500
}
```

**Atomic Write Pattern**:
```python
temp_path = dest_path.with_suffix(".tmp.parquet")
pq.write_table(table, temp_path)
temp_path.replace(dest_path)  # Atomic on POSIX/Windows
```

---

## 8. Dependencies & Integration

### Runtime Dependencies

**Existing** (from README):
- FastAPI, Botasaurus, httpx, Pydantic, python-telegram-bot, sqlite-vec

**Required for Backup** (NOT installed):
```bash
pyarrow>=15.0.0  # Parquet I/O
```

### Environment Variables (NEW)

```bash
# Backup Configuration
BACKUP_ONEDRIVE_DIR=""              # Optional: OneDrive path (fallback: ./backups)
BACKUP_BATCH_SIZE=1000              # Rows per batch
BACKUP_COMPRESSION="zstd"           # Parquet compression
BACKUP_ENABLED="true"               # Master switch
```

### Integration Points

1. **Scraper → Backup**: Automatic call to `export_delta()` after persistence
2. **API → Backup**: Background tasks for export/restore
3. **Database → Parquet**: Explicit PyArrow schemas (no pandas inference)
4. **Lock → Restore**: `browser_lock` prevents concurrent execution

---

## 9. Setup, Build, and Execution

### Prerequisites

- Python 3.10+
- Chrome browser (Botasaurus)
- SQLite 3.35+ (WAL)
- **NEW**: `pyarrow>=15.0.0` (backup)

### Installation (Updated)

```bash
# 1. Install dependencies
pip install -r requirements.txt
pip install pyarrow>=15.0.0  # CRITICAL: Required for backup

# 2. Environment
cp .env.example .env
# Add: BACKUP_ENABLED=true, BACKUP_BATCH_SIZE=1000, etc.

# 3. Database Migration (Manual)
# Run once to apply v9 schema:
python -c "
from database.lifecycle import apply_migrations
apply_migrations('database/migrations', 9)
"

# 4. Start API
python app.py
```

### Running Backup System

**Manual Export**:
```bash
curl -X POST http://localhost:8000/backup/export
```

**Manual Restore**:
```bash
# Ensure scraper is idle
curl -X POST http://localhost:8000/backup/restore
```

**Check Status**:
```bash
curl http://localhost:8000/backup/status
```

**Automatic (Scraper)**:
- Backup runs automatically after scraper persistence
- Disabled if `BACKUP_ENABLED=false` or pyarrow missing

---

## 10. Testing & Validation

### Current Coverage

**Evidence-based gaps**:
- **No test suite**: `tests/` directory absent
- **No pyarrow**: Backup code compiles but cannot run
- **Manual validation only**: Requires live API + SQLite

### Manual Test Cases (Backup)

1. **PyArrow Installation**:
   ```bash
   python -c "import pyarrow; print(pyarrow.__version__)"
   ```
   Expected: >=15.0.0

2. **Backup Export**:
   ```bash
   python -c "from tools.backup.storage import export_delta; print(export_delta())"
   ```
   Should populate `backups/` directory

3. **Backup Restore**:
   ```bash
   python -c "from tools.backup.restore import restore_from_backups; restore_from_backups()"
   ```
   Should reconstruct `scraped_articles` table

4. **Scraper Integration**:
   - Run scraper job
   - Verify `backup/` files created
   - Check watermark updated

### Testing Gaps

- **No unit tests** for backup logic
- **No integration tests** for full pipeline
- **No pyarrow validation** in CI
- **No migration test** for v9

---

## 11. Known Limitations & Non-Goals

### Critical Limitations

1. **PyArrow Dependency Missing**: Backup code is present but non-functional
2. **Manual Migration**: v9 schema not auto-applied
3. **No Delta Validation**: No verification that Parquet files match DB state
4. **Single-Writer Lock**: Restore blocks if scraper running (no queue)
5. **No Incremental Restore**: Full restore only (no selective rollback)

### Hard Constraints

**Parquet Design**:
- **Append-only**: Files never modified, only created
- **Explicit schemas**: No pandas inference (`fixed_size_binary(4096)` required)
- **Atomic writes**: Temp-then-rename prevents corruption

**Backup Scope**:
- **Only scraped_articles + vec**: No jobs, no logs, no batches
- **Deduplication**: `normalized_url` (keep newest)
- **No FTS5 in backup**: Rebuilt on restore

### Architectural Trade-offs

**Pros**:
- Delta exports fast (cursor pagination)
- Immutable Parquet = safe for versioning
- Deduplication on restore prevents bloat

**Cons**:
- Requires pyarrow (heavy dependency)
- No selective restore (all-or-nothing)
- Lock contention (restore blocks scraper)

### Explicit Non-Goals

- **Continuous backup**: Batch only (no real-time)
- **Cloud sync**: OneDrive optional, not mandatory
- **Partial restore**: Full restore only
- **Backup verification**: No checksums or validation

---

## 12. Change Sensitivity

### Extremely Fragile Components

1. **`tools/backup/storage.py`**
   - **Watermark timestamp format**: MUST be `str`, not `datetime`
   - **Filename sanitization**: Spaces → underscores for lexicographical safety
   - **Atomic write**: `.tmp` → replace pattern critical

2. **`tools/scraper/persistence.py`**
   - **UPSERT logic**: `ON CONFLICT(normalized_url)` must preserve `vec_rowid`
   - **updated_at**: Trigger handles auto-update; manual sets cause drift

3. **`tools/scraper/tool.py`**
   - **Early lock release**: MUST occur immediately after scraping
   - **job_items checks**: `SELECT status` for resume idempotency
   - **Backup integration**: Call site must handle "Disabled" gracefully

4. **`api/routes.py`**
   - **Restore lock check**: `browser_lock.locked()` prevents concurrent execution
   - **Background tasks**: Export/restore must not block API

### Tightly Coupled Areas

- **Schema v9**: `updated_at` + trigger required for delta logic
- **PyArrow schemas**: `fixed_size_binary(4096)` must match embedding size
- **Batch size**: `BACKUP_BATCH_SIZE` impacts memory and performance
- **Compression**: `BACKUP_COMPRESSION` affects file size and speed

### Easy Extension Points

- **Batch size tuning**: `BACKUP_BATCH_SIZE` environment variable
- **Compression**: `BACKUP_COMPRESSION` (zstd, snappy, gzip)
- **New backup targets**: Extend `export_delta_batches()` for other tables
- **OneDrive sync**: `BACKUP_ONEDRIVE_DIR` (just change path)

### Hardest Refactors

- **Schema v10**: Would require migration v10 + update all backup schemas
- **Remove pyarrow**: Rewrites entire backup system
- **Async backup**: Would need rewrite to avoid blocking
- **Multi-table backup**: Requires schema redesign