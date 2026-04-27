# AnythingTools - Deterministic Tool Hosting with Browser Lifecycle Management

## 1. Project Overview

AnythingTools is a FastAPI-based deterministic tool hosting service that provides web scraping, publishing, batch reading, and backup capabilities via a REST API. The system executes tools in isolated threads with a single-writer database architecture (SQLite WAL mode) and structured callback delivery.

**Primary Operational Capabilities:**

- **Web Scraper**: DOM-validated extraction, ULID-based identification, automatic delta backup post-persistence, configurable target site registry via `tools/scraper/` (extraction.py, curation.py, persistence.py)
- **Publisher**: Telegram message delivery with state management and crash recovery via `tools/publisher/`
- **Batch Reader**: Hybrid semantic search combining vector embeddings (FTS5) and full-text search via `tools/batch_reader/`
- **Backup System**: Streaming Parquet export/import with OOM-safe chunking (500 rows/batch), watermark-based delta, FTS5 post-restore rebuild via `tools/backup/`

**Explicit Non-Capabilities:**

- **No continuous/real-time backup**: Batch-only execution, manual or triggered
- **No selective restore**: All-or-nothing restoration for master tables only
- **No telemetry**: Local SQLite only, no external metrics collection
- **No concurrent writers**: Single background writer thread (max 1000 queued tasks)
- **No automatic schema migration**: Manual reconciliation via reconciler
- **No backup verification**: No checksums or corruption detection
- **No FTS backup**: FTS tables excluded from restores, rebuilt synchronously

## 2. High-Level Architecture

### Core Components

**1. API Layer (`app.py`, `api/`)**
- FastAPI lifespan manager for startup/shutdown orchestration
- Static file server mounted at `/artifacts` and `/api/artifacts`
- Background job execution for export/restore operations
- **Key Endpoints**:
  - `POST /api/tools/{tool}` - Enqueue tool execution (with circuit breaker for browser tools)
  - `GET /api/jobs/{id}` - Job status with logs
  - `POST /api/backup/export` - Manual backup trigger
  - `POST /api/backup/restore` - Manual restore (requires browser_lock)
  - `GET /api/backup/status` - Backup directory status
  - `GET /api/metrics` - System metrics (queue, active jobs)

**2. Browser Lifecycle Management (`utils/browser_daemon.py`)**
- **ChromeDaemonManager**: Centralized singleton managing all browser operations
- **Health State Machine**: `INITIALIZING` → `READY` → `DEGRADED` → `CRITICAL_FAILURE`
- **Surgical Process Management**: Kills only Chrome processes matching `CHROME_USER_DATA_DIR` via `psutil`
- **PID Auditing**: Logs spawned Chrome PID on every initialization
- **Deep Warmup**: 3-phase verification (Navigation → SoM → Vision) before marking `READY`
- **Legacy Accessors**: Backward-compatible functions (`get_or_create_driver()`, etc.)

**3. Startup Orchestration (`utils/startup/`)**
- **Three-Tier Pipeline** (`__init__.py`):
  - **Tier 1 (Concurrent)**: Artifacts mounting, zombie cleanup, temp cleanup, DB writer init, Telegram handshake
  - **Tier 2 (Sequential)**: DB migrations (reconciliation), vec0 validation
  - **Tier 3 (Concurrent)**: Tool registry load, browser warmup
- **Core Components**:
  - `core.py`: `StartupOrchestrator` with tiering support, failure propagation
  - `cleanup.py`: Zombie Chrome process and temp file cleanup
  - `server.py`: Dynamic artifacts directory mounting from config
  - `database.py`: Pragmas, writer initialization, lifecycle runner, vec0 validation
  - `registry.py`: Whitelisted tool discovery (scraper, draft_editor, publisher, batch_reader)
  - `browser.py`: Deep warmup with 90s timeout, failure → `sys.exit(1)`
  - `telegram.py`: Async orphan handshake for Telegram bot token

**4. Worker Manager (`bot/engine/worker.py`)**
- `UnifiedWorkerManager`: Polls database every 1s for `QUEUED`, `INTERRUPTED`, `PENDING_CALLBACK` jobs
- Thread-isolated tool execution with cancellation flags
- Callback delivery with exponential backoff (3 attempts max)
- **Job lifecycle**: `QUEUED` → `RUNNING` → `COMPLETED|FAILED|PARTIAL|PENDING_CALLBACK|INTERRUPTED`
- **Recovery**: Automatically requeues interrupted jobs on restart

**5. Database Layer (`database/`)**
- **Single-writer background thread** (`writer.py`) with bounded queue (max 1000)
- **WAL mode** for concurrent readers
- **Schema v9** with `updated_at` tracking for delta backups
- **Tables**:
  - *Master*: `scraped_articles`, `scraped_articles_vec`, `long_term_memories`, `long_term_memories_vec`
  - *Non-master*: `jobs`, `job_items`, `job_logs`, `broadcast_batches`
- **Schema Reconciliation** (`reconciler.py`): Detects drift, performs pre-drop snapshots, cascades FK recreations
- **FTS5 Handling**: Excluded from standard reconciliation, created via dedicated existence-based checks

**6. Tool Layer (`tools/`)**
- **Scraper**: Full pipeline (extraction → curation → persistence → auto-backup) with job_items tracking
- **Publisher**: Telegram delivery with state management via job_items
- **Batch Reader**: Hybrid vector + FTS5 search
- **Backup**: Multi-table Parquet export/import with streaming
- **Registry** (`registry.py`): Whitelisted core tools only: `scraper`, `draft_editor`, `publisher`, `batch_reader`

**7. Backup System (`tools/backup/`)**
- **Config**: OOM-safe batch size ceiling (10,000 rows)
- **Schema**: PyArrow schemas with binary embeddings (variable-length, was fixed)
- **Exporter** (`exporter.py`): Chunked 500-row SQL reads, parameterized queries, FTS exclusion
- **Storage** (`storage.py`): Atomic writes, embedding validation, ISO-8601 watermarks
- **Restore** (`restore.py`): Single-writer queue routing, adaptive column mapping, synchronous FTS rebuild
- **Runner** (`runner.py`): Read-only connection, ISO-8601 timestamps, browser_lock acquisition

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
└─ warmup_browser → deep verification → sys.exit(1) on failure
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

## 3. Repository Structure

```
./
├── api/                      # FastAPI routes + schemas
│   ├── routes.py            # All endpoints with job/backup logic (circuit breaker at line 57-64)
│   ├── schemas.py           # Pydantic models (watermark support)
│   ├── telegram_client.py   # Bot API + orphan handshake
│   └── telegram_notifier.py # Message delivery
├── bot/                     # Worker engine
│   ├── engine/
│   │   ├── worker.py        # UnifiedWorkerManager (threads, handles INTERRUPTED jobs)
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
│   │   ├── __init__.py      # MASTER_TABLES, ALL_FTS_TABLES (rules enforced)
│   │   ├── vector.py        # FTS5 triggers + vec0 tables
│   │   └── *.py             # jobs, finance, pdf, token
│   ├── reconciler.py        # Schema drift detection + repair
│   ├── schema_introspector.py # PRAGMA parsing + DDL comparison
│   ├── lifecycle.py         # Reconciler wrapper + recovery
│   ├── writer.py            # Background single-writer thread (queue max 1000)
│   ├── connection.py        # DB manager (optional vec0, query_only)
│   ├── health.py            # Table validation
│   └── *.py                 # reader, job_queue, blackboard, formula_cache
├── deprecated/              # Legacy code (~70% volume, never loaded)
│   ├── bot/                 # Old agent/weaver/modes
│   └── tools/               # Old research, finance, polymarket, quiz.
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
│   ├── startup/             # Modular startup system
│   │   ├── core.py          # StartupOrchestrator (tiers)
│   │   ├── cleanup.py       # Zombie chrome + temp files
│   │   ├── server.py        # Artifacts mounting
│   │   ├── database.py      # Pragmas, writer, lifecycle, vec0
│   │   ├── registry.py      # Tool registry loading
│   │   ├── browser.py       # Warmup (90s timeout + deep verification)
│   │   ├── telegram.py      # Async handshake
│   │   └── __init__.py      # Pipeline assembly
│   ├── browser_daemon.py    # Browser driver management (ChromeDaemonManager)
│   ├── browser_lock.py      # Lock for restore operations
│   ├── logger/              # Dual logging system
│   ├── som_utils.py         # SoM injection, overlay removal, single-tab enforcement
│   ├── vision_utils.py      # Screenshot capture, slicing, optimization
│   └── *.py                 # security, helpers, etc.
├── tests/                   # Unit tests
│   ├── test_backup.py       # Schema, validation, Pydantic compat
│   └── test_browser_e2e.py  # Browser automation
├── app.py                   # FastAPI entrypoint (refactored with 2-phase shutdown)
├── config.py                # API key and global configuration
└── requirements.txt         # Dependencies
```

### Non-Obvious Structures
- **`deprecated/`** - 70% repository volume, imports disabled, never executed. Contains legacy tools (finance, research, polymarket, quiz) and old bot architecture.
- **`tests/`** - Unit tests for backup system and browser E2E only
- **No automatic migration**: Manual schema changes via reconciler only
- **`tools/scraper/prompts.py`** - Canonical prompt module after PLAN-02 (replaced `prompt.py`)
- **`utils/browser_daemon.py`** - New singleton manager replacing module-level globals (as of this update)

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

**8. ChromeDaemonManager Health States**
- **INITIALIZING**: Browser process starting, warmup not yet run
- **READY**: Deep warmup passed (Navigation, SoM, Vision tests successful)
- **DEGRADED**: Not currently used but reserved for future states
- **CRITICAL_FAILURE**: Warmup failed or shutdown initiated

**9. Deep Warmup Verification**
- **Phase 1 (Navigation)**: Navigate to Space Jam 1996, verify marker string "SPACE JAM, characters, names, and all related" exists (case-insensitive)
- **Phase 2 (SoM)**: Inject data-ai-id markers with vertical displacement logic, verify count > 1
- **Phase 3 (Vision)**: Capture screenshot, slice if needed, verify valid slices
- **Failure Policy**: Any failure → CRITICAL log → `sys.exit(1)` → application shutdown

**10. Two-Phase Shutdown (app.py)**
- **Phase 1**: Stop worker manager polling, broadcast cancellation to existing workers
- **Phase 2**: 60-second drain timer, release browser resources, shutdown DB writer
- **Thread-Safety**: Uses `list()` snapshot for cancellation flags to prevent RuntimeError

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

### 5.4 Scraper Pipeline

**Tool Execution** (`tools/scraper/tool.py`):
```python
from tools.scraper.prompts import SCRAPER_SYS_PROMPT, CURATION_SYS_PROMPT
# Uses canonical constants from prompts.py (post-PLAN-02)
```

### 5.5 Browser Environment Management

**ChromeDaemonManager Operations** (`utils/browser_daemon.py`):

*Initialization:*
```python
def _init_driver(self) -> Driver:
    self._status = BrowserStatus.INITIALIZING
    self.surgical_kill()  # Kill existing Chrome for this profile
    
    profile_path = os.path.abspath(config.CHROME_USER_DATA_DIR).replace("\\", "/")
    self._driver = Driver(
        headless=False,
        user_agent="real",  # Normalized to lowercase
        window_size=(1920, 1080),
        arguments=[f"--user-data-dir={profile_path}"],  # Explicit param
    )
    # Log PID
    if hasattr(self._driver, 'browser') and hasattr(self._driver.browser, 'process'):
        self._pid = self._driver.browser.process.pid
        log.dual_log(tag="Browser:Daemon", message=f"Chrome spawned with PID {self._pid}")
    
    # 3-second stabilization delay
    log.dual_log(tag="Browser:Daemon", message="Waiting 3s for Chrome CDP to settle...")
    self._driver.sleep(3)
    
    # Status remains INITIALIZING until deep_warmup() succeeds
```

*Surgical Kill:*
```python
def surgical_kill(self) -> None:
    if psutil is None:
        return
    
    target_dir = os.path.abspath(config.CHROME_USER_DATA_DIR).lower()
    killed = False
    
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        current_pid = proc.pid
        try:
            info = proc.info
            if not info:
                continue
            cmdline = " ".join(info.get('cmdline') or []).lower()
            proc_name = (info.get('name') or "").lower()
            
            if "chrome" in proc_name and target_dir in cmdline:
                proc.kill()
                killed = True
                log.dual_log(tag="Browser:Kill", message=f"Killed Chrome PID {current_pid} for profile {target_dir}")
        except (psutil.AccessDenied, psutil.PermissionError) as e:
            log.dual_log(tag="Browser:Kill", message=f"FATAL: Permission denied killing Chrome PID {current_pid}: {e}", level="CRITICAL")
            raise RuntimeError(f"Permission denied killing Chrome PID: {e}")
        except psutil.NoSuchProcess:
            continue
        except Exception as e:
            log.dual_log(tag="Browser:Kill", message=f"Error killing process: {e}", level="WARNING")
    
    if killed:
        log.dual_log(tag="Browser:Kill", message=f"Surgically killed Chrome processes for {target_dir}")
```

*Deep Warmup:*
```python
def deep_warmup(self) -> bool:
    try:
        from utils.browser_utils import safe_google_get
        from utils.som_utils import reinject_all
        from utils.vision_utils import capture_and_optimize
        
        driver = self.get_or_create_driver()
        
        # Phase 1: Navigation Test - Space Jam 1996
        log.dual_log(tag="Startup:Warmup", message="Phase 1: Navigation Test")
        safe_google_get(driver, "https://www.spacejam.com/1996/")
        driver.short_random_sleep()
        
        # Verify presence of the expected marker string (case-insensitive)
        page_html = (driver.page_html or "").lower()
        expected = "SPACE JAM, characters, names, and all related".lower()
        if expected not in page_html:
            raise RuntimeError("Navigation failed: Content mismatch - expected marker not found")
        
        # Phase 2: SoM Test
        log.dual_log(tag="Startup:Warmup", message="Phase 2: SoM Injection Test")
        reinject_all(driver, self._id_tracking)
        main_range = self._id_tracking.get('main')
        if not main_range or main_range[1] <= 1:
            raise RuntimeError("SoM injection failed: No markers added")
        
        # Phase 3: Vision Test
        log.dual_log(tag="Startup:Warmup", message="Phase 3: Vision Subsystem Test")
        slices = capture_and_optimize(driver, 0)
        if not slices or not any(s.get("b64") for s in slices if s.get("status") == "OK"):
            raise RuntimeError("Vision test failed: No valid slices produced")
        
        log.dual_log(tag="Startup:Warmup", message="Deep Warmup Successful")
        self._status = BrowserStatus.READY
        return True
    except Exception as e:
        log.dual_log(tag="Startup:Warmup", message=f"CRITICAL: Warmup Failed: {e}", level="CRITICAL")
        self._status = BrowserStatus.CRITICAL_FAILURE
        return False
```

**API Circuit Breaker** (`api/routes.py`):
```python
if tool_name in ["scraper", "browser_task"]:
    from utils.browser_daemon import daemon_manager, BrowserStatus
    if daemon_manager.status != BrowserStatus.READY:
        raise HTTPException(
            status_code=503, 
            detail=f"Browser environment is currently {daemon_manager.status.value}. Tool unavailable."
        )
```

**Two-Phase Shutdown** (`app.py`):
```python
finally:
    log.dual_log(tag="App:Shutdown", message="Initiating shutdown sequence...", level="INFO")
    
    try:
        from bot.engine.worker import get_manager
        mgr = get_manager()
        
        # Phase 1: Stop polling, broadcast cancellation
        log.dual_log(tag="App:Shutdown", message="Phase 1: Stopping worker manager polling loop", level="INFO")
        mgr.stop()
        
        log.dual_log(tag="App:Shutdown", message="Phase 2: Broadcasting cancellation to active workers", level="INFO")
        for flag in list(mgr.cancellation_flags.values()):  # Snapshot to prevent RuntimeError
            flag.set()
        
        # Phase 2: Drain (60s)
        drain_start = time.time()
        drain_timeout = 60.0
        log.dual_log(tag="App:Shutdown", message=f"Phase 3: Draining active jobs for up to {drain_timeout}s", level="INFO")
        while mgr._active_jobs and (time.time() - drain_start < drain_timeout):
            remaining = len(mgr._active_jobs)
            log.dual_log(tag="App:Shutdown", message=f"Draining {remaining} active job(s), elapsed: {time.time() - drain_start:.1f}s")
            await asyncio.sleep(2)
        
        if mgr._active_jobs:
            log.dual_log(tag="App:Shutdown", message=f"Drain timeout exceeded, {len(mgr._active_jobs)} job(s) remaining", level="WARNING")
        else:
            log.dual_log(tag="App:Shutdown", message="All active jobs drained successfully", level="INFO")
        
        # Release resources
        log.dual_log(tag="App:Shutdown", message="Releasing browser resources", level="INFO")
        from utils.browser_daemon import daemon_manager
        daemon_manager.shutdown_driver()
        daemon_manager.surgical_kill()
        
        from database.writer import wait_for_writes, shutdown_writer
        await wait_for_writes()
        shutdown_writer()
        
        log.dual_log(tag="App:Shutdown", message="Clean shutdown complete", level="INFO")
    except Exception as e:
        log.dual_log(tag="App:Shutdown", message=f"Shutdown error: {e}", level="ERROR")
    
    if startup_failed:
        os._exit(1)
```

**Warmup Sequence with Failure Policy** (`utils/startup/browser.py`):
```python
async def warmup_browser() -> None:
    """CRITICAL: Deep warmup orchestration. Fatal on failure."""
    def _do_warmup():
        browser_lock.acquire()
        try:
            return daemon_manager.deep_warmup()
        finally:
            browser_lock.safe_release()

    try:
        # 90-second timeout for slow cold-starts and stabilization delay
        success = await asyncio.wait_for(asyncio.to_thread(_do_warmup), timeout=90.0)
        if not success:
            raise RuntimeError("Browser failed internal health checks.")
    except asyncio.TimeoutError:
        raise RuntimeError("Browser warmup timed out after 90 seconds.")
    except Exception as e:
        log.dual_log(tag="Startup:Browser", message=f"Warmup crashed: {e}", level="CRITICAL")
        raise RuntimeError(f"Browser Warmup Failed: {e}")
```

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
API_KEY="dev_default_key_change_me_in_production"
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

## 11. Known Limitations & Non-Goals

### Critical Constraints
1. **Single-Writer**: Restore blocks on active scraper via `browser_lock`
2. **All-or-Nothing**: No selective table restore
3. **Parquet Immutability**: Files never modified, only deleted
4. **No verification**: No checksums
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

**9. `utils/browser_daemon.py` (NEW)**
- **Risk**: State machine correctness, surgical kill accuracy, warmup failure propagation
- **Fix Applied**: 
  - Singleton manager with health states
  - PID-aware process filtering
  - Deep warmup with sys.exit(1) on failure
  - Thread-safe legacy accessors

**10. `app.py` (MODIFIED)**
- **Risk**: Shutdown race conditions, missing browser cleanup
- **Fix Applied**: 
  - Two-phase shutdown (polling stop → cancellation → drain → cleanup)
  - Thread-safe iteration with `list()`
  - Surgical kill integration

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