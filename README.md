# AnythingTools - Deterministic Tool Hosting with Browser Lifecycle Management

## 1. Project Overview

AnythingTools is a FastAPI-based deterministic tool hosting service that provides web scraping, publishing, batch reading, and backup capabilities via a REST API. The system executes tools in isolated threads with a single-writer database architecture (SQLite WAL mode) and structured callback delivery.

**Primary Operational Capabilities:**

- **Web Scraper**: DOM-validated extraction using Set-of-Marks (SoM) instrumentation, ULID-based identification, automatic delta backup post-persistence, configurable target site registry via `tools/scraper/`
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
- **No proactive Telegram**: Passive-only output sink (no handshakes or startup messages)

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
- **ChromeDaemonManager**: Centralized singleton managing all browser operations with Botasaurus 4.x
- **Health State Machine**: `INITIALIZING` → `READY` → `DEGRADED` → `CRITICAL_FAILURE`
- **Surgical Process Management**: Kills only Chrome processes matching `CHROME_USER_DATA_DIR` via `psutil`
- **PID Auditing**: Logs spawned Chrome PID on every initialization using `._browser._process_pid`
- **Deep Warmup**: 3-phase verification (Navigation → SoM → Vision) before marking `READY`
  - Phase 1: Navigate to `https://www.spacejam.com/1996/` with marker verification
  - Phase 2: SoM injection test with timeout handling
  - Phase 3: Vision subsystem screenshot capture
- **Legacy Accessors**: Backward-compatible functions (`get_or_create_driver()`, etc.)

**3. SoM Architecture (NEW - PLAN-01)**
- **SoM Injector** (`utils/som_injector.py`): Chunked injection engine with bounded execution
  - `WatchdogTimer`: Cooperative threading timeout for `run_js()` calls
  - `SoMCriticalTimeoutError`: Signals thread-blocking JavaScript hangs
  - `BadgePositionCalculator`: Python-based O(n²) spatial overlap resolution
  - **Three-phase injection**:
    1. Scan via JS with `getComputedStyle()` opacity/visibility verification
    2. Calculate positions in Python (avoiding DOM reflows)
    3. Batched mutation via JS with Indexed ID tracking
- **Orchestrator Core** (`bot/orchestrator_core/`):
  - `OrchestratorRouter`: State machine coordinating context building, budget enforcement, tool execution
  - `SoMContextBuilder`: Tracks bounded context, marker ranges, element hints
  - `BudgetEnforcer`: FIFO eviction for LLM context window management
- **Integration**: `run_tool_with_orchestrator()` in `bot/engine/tool_runner.py`
- **Cleanup**: `clear_job_tracking()` prevents memory leaks across job sessions

**4. Startup Orchestration (`utils/startup/`)**
- **Three-Tier Pipeline** (`__init__.py`):
  - **Tier 1 (Concurrent)**: Artifacts mounting, zombie cleanup, temp cleanup, DB writer init
  - **Tier 2 (Sequential)**: DB migrations (reconciliation), vec0 validation
  - **Tier 3 (Concurrent)**: Tool registry load, browser warmup
- **Core Components**:
  - `core.py`: `StartupOrchestrator` with tiering support, failure propagation
  - `cleanup.py`: Zombie Chrome process and temp file cleanup
  - `server.py`: Dynamic artifacts directory mounting from config
  - `database.py`: Pragmas, writer initialization, lifecycle runner, vec0 validation
  - `registry.py`: Whitelisted tool discovery (`scraper`, `draft_editor`, `publisher`, `batch_reader`)
  - `browser.py`: Deep warmup with 90s timeout, failure → `sys.exit(1)`
  - `telegram.py`: **REMOVED** (as of PLAN-01) - No proactive handshake

**5. Worker Manager (`bot/engine/worker.py`)**
- `UnifiedWorkerManager`: Polls database every 1s for `QUEUED`, `INTERRUPTED`, `PENDING_CALLBACK` jobs
- Thread-isolated tool execution with cancellation flags
- Callback delivery with exponential backoff (3 attempts max)
- **Job lifecycle**: `QUEUED` → `RUNNING` → `COMPLETED|FAILED|PARTIAL|PENDING_CALLBACK|INTERRUPTED`
- **Recovery**: Automatically requeues interrupted jobs on restart

**6. Database Layer (`database/`)**
- **Single-writer background thread** (`writer.py`) with bounded queue (max 1000)
- **WAL mode** for concurrent readers
- **Schema v9** with `updated_at` tracking for delta backups
- **Tables**:
  - *Master*: `scraped_articles`, `scraped_articles_vec`, `long_term_memories`, `long_term_memories_vec`
  - *Non-master*: `jobs`, `job_items`, `job_logs`, `broadcast_batches`
- **Schema Reconciliation** (`reconciler.py`): Detects drift, performs pre-drop snapshots, cascades FK recreations
- **FTS5 Handling**: Excluded from standard reconciliation, created via dedicated existence-based checks

**7. Tool Layer (`tools/`)**
- **Scraper**: Full pipeline (extraction → curation → persistence → auto-backup) with SoM integration
- **Publisher**: Telegram delivery with state management via job_items (passive sink only)
- **Batch Reader**: Hybrid vector + FTS5 search
- **Backup**: Multi-table Parquet export/import with streaming
- **Registry** (`registry.py`): Whitelisted core tools only with MCP-style schema

**8. Backup System (`tools/backup/`)**
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
- **SoM Orchestration**: State machine with budget enforcement

### Data Flow

**Tool Execution with Orchestration:**
```
[API Request] → POST /api/tools/{tool}
    ↓
[Job Queue] → QUEUED status
    ↓
[Worker Poll] → UnifiedWorkerManager dequeues
    ↓
[run_tool_with_orchestrator()]
    ├─ Context Building: SoMContextBuilder.initialize()
    ├─ If browser tool:
    │   ├─ Get driver: daemon_manager.get_or_create_driver()
    │   ├─ Wait DOM stability: wait_for_dom_stability()
    │   ├─ SoM Injection: inject_som() → WatchdogTimer → SoMInjector
    │   │   ├─ Scan with getComputedStyle() verification
    │   │   ├─ Python position calculation
    │   │   └─ Batched JS mutation
    │   ├─ Store markers: context_builder.inject_som_markers()
    │   └─ Exception handling: SoMCriticalTimeoutError → surgical_kill()
    └─ Execute: tool_executor() with context + hints
        ↓
[Tool Logic] → Pass through orchestrator
    ↓
[Cleanup] → browser_daemon.clear_job_tracking() (finally block)
    ↓
[Database] → Enqueue writes via single-writer
    ↓
[Callback] → Deliver result with exponential backoff
```

**Startup Pipeline (Three-Tier):**
```
Tier 1 (Concurrent):
├─ mount_artifacts → config.ARTIFACTS_DIR
├─ cleanup_zombie_chrome → psutil.scan + kill
├─ cleanup_temp_files → remove *.tmp.parquet
├─ init_database_layer → pragmas + start_writer()
└─ [REMOVED] start_telegram_handshake

Tier 2 (Sequential):
├─ run_db_migrations → lifecycle.reconcile()
└─ validate_vec0 → verify extension / fallback

Tier 3 (Concurrent):
├─ load_tool_registry → whitelist + validation
└─ warmup_browser → deep verification (Space Jam → SoM → Vision)
    ├─ Phase 1: Navigate + verify "SPACE JAM" marker
    ├─ Phase 2: SoM injection test
    ├─ Phase 3: Vision screenshot capture
    └─ Failure: CRITICAL log + sys.exit(1)
```

## 3. Repository Structure

```
./
├── api/                      # FastAPI routes + schemas
│   ├── routes.py            # All endpoints with job/backup logic
│   ├── schemas.py           # Pydantic models
│   ├── telegram_client.py   # Bot API (passive only, NO handshake method)
│   └── telegram_notifier.py # Message delivery (passive sink)
├── bot/                     # Worker engine
│   ├── engine/
│   │   ├── worker.py        # UnifiedWorkerManager (threads, INTERRUPTED)
│   │   └── tool_runner.py   # Job execution + run_tool_with_orchestrator()
│   ├── core/
│   │   └── constants.py     # Job status enums
│   └── orchestrator_core/   # NEW: SoM-aware orchestration
│       ├── router.py        # OrchestratorRouter (state machine)
│       ├── context.py       # SoMContextBuilder (tracking)
│       ├── eviction.py      # BudgetEnforcer (FIFO)
│       └── __init__.py      # Package exports
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
│   ├── connection.py        # DB manager
│   ├── health.py            # Table validation
│   └── *.py                 # reader, job_queue, blackboard, formula_cache
├── deprecated/              # Legacy code (70% volume, never loaded)
│   ├── bot/                 # Old agent/weaver/modes
│   └── tools/               # Old research, finance, polymarket, quiz
├── tools/                   # Tool implementations
│   ├── scraper/             # Extraction + curation + persistence
│   │   ├── prompts.py       # Canonical prompts (post-PLAN-02)
│   │   ├── Skill.py         # Tool descriptor
│   │   ├── tool.py          # Main tool
│   │   ├── browser.py       # DOM helpers
│   │   ├── curation.py      # Article selection
│   │   └── extraction.py    # Content extraction (SoM integration)
│   ├── publisher/           # Telegram delivery (passive only)
│   ├── batch_reader/        # Hybrid search
│   ├── backup/              # Hardened backup system
│   │   ├── config.py        # Batch ceiling (10k), OOM rules
│   │   ├── models.py        # Watermark/Result (Pydantic compat)
│   │   ├── schema.py        # PyArrow schemas
│   │   ├── exporter.py      # Parameterized queries, FTS exclusion
│   │   ├── storage.py       # Atomic writes + embedding validation
│   │   ├── restore.py       # enqueue_transaction + sync FTS
│   │   └── runner.py        # Read-only connection
│   ├── draft_editor/        # Content editing tool
│   ├── base.py              # BaseTool
│   └── registry.py          # Whitelisted tool discovery + MCP schema
├── utils/                   # Infrastructure
│   ├── startup/             # Modular startup system
│   │   ├── core.py          # StartupOrchestrator (tiers)
│   │   ├── cleanup.py       # Zombie chrome + temp files
│   │   ├── server.py        # Artifacts mounting
│   │   ├── database.py      # Pragmas, writer, lifecycle, vec0
│   │   ├── registry.py      # Tool registry loading
│   │   ├── browser.py       # Warmup (90s timeout + deep verification)
│   │   └── __init__.py      # Pipeline assembly (NO telegram import)
│   ├── browser_daemon.py    # Browser driver management (Botasaurus 4.x)
│   ├── browser_lock.py      # Lock for restore operations
│   ├── logger/              # Dual logging system
│   ├── som_utils.py         # SoM injection (delegates to SoMInjector)
│   ├── som_injector.py      # NEW: Chunked injection with watchdog
│   ├── vision_utils.py      # Screenshot capture, slicing
│   └── *.py                 # security, helpers, etc.
├── tests/                   # Unit tests
│   ├── test_backup.py       # Schema, validation, Pydantic compat
│   └── test_browser_e2e.py  # Browser automation
├── app.py                   # FastAPI entrypoint
├── config.py                # API key and global configuration
└── requirements.txt         # Dependencies
```

### Non-Obvious Structures
- **`deprecated/`** - 70% repository volume, imports disabled, never executed
- **`tests/`** - Unit tests for backup system and browser E2E only
- **No automatic migration**: Manual schema changes via reconciler only
- **`utils/som_injector.py`** - NEW: Replaces monolithic JS injection with chunked architecture
- **`bot/orchestrator_core/`** - NEW: State machine orchestration for SoM-aware execution
- **`api/telegram_client.py`** - REMOVED: `run_orphan_handshake()` method (passive-only)
- **`utils/startup/telegram.py`** - DELETED: No longer imported or executed

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

**8. ChromeDaemonManager Health States (Updated for SoM)**
- **INITIALIZING**: Browser process starting, warmup not yet run
- **READY**: Deep warmup passed (Navigation, SoM, Vision tests successful)
- **DEGRADED**: Reserved for future states
- **CRITICAL_FAILURE**: Warmup failed, SoM hung, or shutdown initiated → surgical kill

**9. Deep Warmup Verification (Space Jam Required)**
- **Phase 1 (Navigation)**: Navigate to `https://www.spacejam.com/1996/`, verify marker string "SPACE JAM, characters, names, and all related" exists (case-insensitive)
- **Phase 2 (SoM)**: Inject data-ai-id markers, verify count > 1, **handles timeouts with surgical kill**
- **Phase 3 (Vision)**: Capture screenshot, slice if needed, verify valid slices
- **Failure Policy**: Any failure → CRITICAL log → `sys.exit(1)` or `surgical_kill()` → application shutdown

**10. Two-Phase Shutdown (app.py)**
- **Phase 1**: Stop worker manager polling, broadcast cancellation to existing workers
- **Phase 2**: 60-second drain timer, release browser resources, shutdown DB writer
- **Thread-Safety**: Uses `list()` snapshot for cancellation flags to prevent RuntimeError

**11. Set-of-Marks (SoM) Context (NEW - PLAN-01)**
- **Data-ai-id attributes**: Sequential injection on visible elements
- **Badge positions**: Calculated in Python to avoid DOM reflows
- **Marker ranges**: Stored in job context for LLM hints
- **Element hints**: Extracted and passed to tools
- **Visibility checks**: `window.getComputedStyle(el)` for `opacity !== '0'` and `visibility !== 'hidden'`

**12. Budget Enforcement (NEW - PLAN-01)**
- **LLM Context Limit**: Configurable (default 800,000 chars)
- **FIFO Eviction**: Oldest context removed when budget exceeded
- **Single-item truncation**: If one item exceeds budget, truncate to 1000 chars
- **Applied at**: Orchestration layer before LLM calls

**13. Watchdog Timeout (NEW - PLAN-01)**
- **Cooperative Timer**: Threading event set by timer thread
- **Applied to**: All `run_js()` calls in SoM injection
- **Timeout**: Configurable (default 60.0 seconds)
- **Action on Timeout**: `SoMCriticalTimeoutError` → `surgical_kill()` → `CRITICAL_FAILURE`
- **Prevents**: Python thread leaks from infinite JavaScript execution

**14. Passive Telegram Sink (PLAN-01 Cleanup)**
- **No handshake**: `run_orphan_handshake()` method removed
- **No startup messages**: `start_telegram_handshake()` task deleted
- **No proactive sending**: All Telegram messages are user/job-initiated
- **Enforcement**: Documented in `utils/telegram/__init__.py` architectural rule

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

**SoM Injection Architecture** (`utils/som_injector.py`):
```python
class SoMInjector:
    # Three-phase injection with watchdog
    _SCAN_JS = "..."  # With getComputedStyle() verification
    _MARK_IDS_JS_TEMPLATE = "..."  # Batched ID assignment
    _MARK_BADGES_JS_TEMPLATE = "..."  # Batched badge creation
    
    def inject(self, start_id, mode):
        with WatchdogTimer(self._timeout) as wd:
            total_tagged = self._driver.run_js(self._TAG_SCAN_INDICES_JS)
            if wd.timed_out:
                raise SoMCriticalTimeoutError(...)
        # ... calculate positions in Python, batch mutations
```

**Orchestrator Router** (`bot/orchestrator_core/router.py`):
```python
class OrchestratorRouter:
    def __init__(self, job_id: str, budget: int | None = None):
    
    async def run(self, tool_name, tool_args, tool_executor, browser_daemon, **kwargs):
        try:
            # Context building
            context_builder = SoMContextBuilder(self._job_id)
            context_builder.initialize(tool_name, tool_args)
            
            # SoM injection if browser tool
            if browser_daemon:
                last_id = inject_som(driver, start_id=1)
                context_builder.inject_som_markers((1, last_id - 1))
            
            # Tool execution with context
            result = await tool_executor(tool_name, tool_args, **kwargs)
            
        except SoMCriticalTimeoutError:
            browser_daemon.surgical_kill()
            raise RuntimeError("SoM Injection hung. Browser killed.")
            
        finally:
            if browser_daemon:
                browser_daemon.clear_job_tracking()  # Memory leak prevention
```

## 5. Detailed Behavior

### 5.1 SoM Injection with Watchdog & Chunking

**Flow** (`utils/som_injector.py`, `utils/som_utils.py`):
```python
# 1. Tag Scan Indices (with watchdog)
total_tagged = driver.run_js(SoMInjector._TAG_SCAN_INDICES_JS)
# → 60s timeout enforced → raises SoMCriticalTimeoutError if hung

# 2. Scan Visible Elements (with visibility checks)
raw = driver.run_js(SoMInjector._SCAN_JS)
# → Uses window.getComputedStyle(el)
# → Skips opacity=0, visibility=hidden elements

# 3. Python Position Calculation
elements = [ElementInfo(...)]
badge_positions = BadgePositionCalculator.compute_positions(elements)
# → O(n²) overlap displacement: +15px if within threshold

# 4. Batched ID Assignment
for batch in elements:
    mark_data = [{"scanIdx": ..., "aiId": current_id}]
    driver.run_js(SoMInjector._MARK_IDS_JS_TEMPLATE)
    current_id += len(batch)

# 5. Batched Badge Injection (if FULL mode)
for batch in elements:
    badge_data = [{"aiId": ..., "top": ..., "left": ...}]
    driver.run_js(SoMInjector._MARK_BADGES_JS_TEMPLATE)

# 6. Cleanup
driver.run_js("removeAttribute('data-ai-scan-idx')")
```

**Graceful Degradation** (`utils/som_utils.py`):
```python
def inject_som(driver, start_id):
    try:
        return injector.inject(start_id, mode=FULL)
    except SoMCriticalTimeoutError:
        raise  # Propagated to orchestrator for surgical kill
    except Exception as e:
        log.warning(f"Full failed, retrying marker-only: {e}")
        return injector.inject(start_id, mode=MARKER_ONLY)
```

### 5.2 Domain-Specific Execution

**ChromeDaemonManager Lifecycle** (`utils/browser_daemon.py`):
```python
# Initialization
driver = Driver(
    headless=False,
    user_agent="real",
    window_size=(1920, 1080),
    arguments=[f"--user-data-dir={profile_path}"]
)

# PID Capture (Botasaurus 4.x)
if hasattr(driver, '_browser') and hasattr(driver._browser, '_process_pid'):
    self._pid = driver._browser._process_pid  # NEW attribute path

# Deep Warmup with SoM + Timeout
def deep_warmup(self):
    # Phase 1: Navigation to Space Jam
    safe_google_get(driver, "https://www.spacejam.com/1996/")
    
    # Phase 2: SoM Test with Exception Handling
    try:
        reinject_all(driver, self._id_tracking)
    except SoMCriticalTimeoutError:
        log.critical("JS hung during SoM injection")
        self.surgical_kill()
        self._status = CRITICAL_FAILURE
        raise RuntimeError("Chrome killed due to infinite loop")
    
    # Phase 3: Vision
    capture_and_optimize(driver, 0)
```

**Orchestration Flow** (`bot/engine/tool_runner.py`):
```python
async def run_tool_with_orchestrator(tool_name, args, telemetry, job_id, **kwargs):
    # 1. Resolve browser daemon if tool needs it
    browser_daemon = None
    if tool_name in ["scraper", "browser_task"]:
        if daemon_manager.status.value == "READY":
            browser_daemon = daemon_manager
    
    # 2. Create router
    router = OrchestratorRouter(job_id)
    
    # 3. Execute with full orchestration
    return await router.run(
        tool_name=tool_name,
        tool_args=args,
        tool_executor=execute_tool,  # Wraps run_tool_safely
        browser_daemon=browser_daemon,
        **kwargs
    )
```

**Tool Execution with Context** (`tools/scraper/extraction.py`):
```python
# Engagement scroll with error handling
try:
    wait_for_dom_stability(driver)
    try:
        last_id = inject_som(driver, start_id=1)
    except SoMCriticalTimeoutError:
        daemon_manager.surgical_kill()
        raise RuntimeError("JS injection hung. Browser killed.")
    
    if last_id > 1:
        target_id = random.randint(1, last_id - 1)
        element = driver.select(f'[data-ai-id="{target_id}"]')
        if element:
            element.scroll_into_view()
except Exception as e:
    log.debug(f"Engagement failed: {e}")
```

### 5.3 Error Handling & State Transitions

**On SoM Timeout**:
1. `WatchdogTimer` expires → `_timed_out.set()`
2. `run_js()` completes or continues but flag is set
3. `SoMCriticalTimeoutError` raised
4. Orchestrator catches → calls `browser_daemon.surgical_kill()`
5. Sets `browser_daemon._status = CRITICAL_FAILURE`
6. Application either aborts or reinitializes

**On Telegram Cleanup (PLAN-01)**:
1. `run_orphan_handshake()` method deleted from `api/telegram_client.py`
2. `utils/startup/telegram.py` file deleted
3. `utils/startup/__init__.py` removes import and Tier 1 task
4. `utils/telegram/__init__.py` adds architectural rule comment
5. No proactive messages ever sent

**On Memory Leak Prevention**:
1. `OrchestratorRouter.run()` has `finally` block
2. `browser_daemon.clear_job_tracking()` called regardless of success
3. `_id_tracking.clear()` under lock
4. Prevents accumulation across job executions

## 6. Public Interfaces

### API Endpoints

**Tool Execution** (`POST /api/tools/{tool}`):
```bash
curl -X POST http://localhost:8000/api/tools/scraper \
  -H "Content-Type: application/json" \
  -d '{
    "targets": [{"url": "https://example.com", "selectors": ["article"]}],
    "job_metadata": {"user_id": "123"}
  }'
```
Response:
```json
{
  "status": "QUEUED",
  "job_id": "01H7Y...",
  "message": "Job enqueued successfully"
}
```
- Circuit breaker active for browser tools if daemon not READY

**Job Status** (`GET /api/jobs/{id}`):
```bash
curl http://localhost:8000/api/jobs/01H7Y...
```
Response:
```json
{
  "status": "COMPLETED",
  "logs": [...],
  "final_payload": {"articles_scraped": 5}
}
```

**Backup Export** (`POST /api/backup/export`):
```bash
curl -X POST http://localhost:8000/api/backup/export \
  -H "Content-Type: application/json" \
  -d '{"mode": "delta"}'
```
Response:
```json
{"status": "EXPORT_QUEUED", "job_id": "01H7Y..."}
```

**Backup Restore** (`POST /api/backup/restore`):
```bash
curl -X POST http://localhost:8000/api/backup/restore \
  -H "Content-Type: application/json" \
  -d '{"watermark": "2024-01-01T00:00:00"}'
```
Response:
```json
{"status": "RESTORE_QUEUED", "job_id": "01H7Z..."}
```

**System Metrics** (`GET /api/metrics`):
```bash
curl http://localhost:8000/api/metrics
```
Response:
```json
{
  "queue_length": 3,
  "active_jobs": 2,
  "browser_status": "READY",
  "chrome_pid": 12345
}
```

### Python/CLI

**Manual Tool Trigger**:
```python
from tools.registry import REGISTRY
from bot.engine.tool_runner import run_tool_with_orchestrator

# Registry provides whitelist + schema
REGISTRY.load_all()
REGISTRY.schema_list()  # MCP-style for LLM discovery
REGISTRY.get_som_tools()  # ["scraper", "browser_task"]

# Execute via orchestrator (integrates SoM)
async def run_example():
    result = await run_tool_with_orchestrator(
        tool_name="scraper",
        args={"targets": [...]},
        telemetry=None,  # Reserved
        job_id="manual_001",
        browser_daemon=daemon_manager
    )
```

**System Operations**:
```python
from utils.browser_daemon import daemon_manager

# Status check
print(daemon_manager.status.value)  # "READY", "CRITICAL_FAILURE", etc.

# Surgical kill on demand
daemon_manager.surgical_kill()

# Track cleanup
daemon_manager.clear_job_tracking()
```

**Backup Operations**:
```python
from tools.backup.runner import run_backup_export

# From API or CLI
run_backup_export(mode="delta")
```

## 7. State, Persistence, and Data

### Database Schema (v9)

**Master Tables** (protected, restored):
```sql
CREATE TABLE scraped_articles (
    id TEXT PRIMARY KEY,  -- ULID
    normalized_url TEXT UNIQUE,
    vec_rowid INTEGER,    -- Reference to vectors
    updated_at TEXT,      -- ISO-8601
    -- ... content fields
);

CREATE TABLE scraped_articles_vec (
    rowid INTEGER PRIMARY KEY,
    embedding BLOB,       -- vec0 binary
    source_id TEXT,       -- FK to scraped_articles.id
    updated_at TEXT
);

CREATE TABLE long_term_memories (...);
CREATE TABLE long_term_memories_vec (...);
```

**Non-Master Tables** (not restored, rebuilt):
```sql
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    tool_name TEXT,
    status TEXT,          -- QUEUED, RUNNING, etc.
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE job_items (...);
CREATE TABLE job_logs (...);
CREATE TABLE broadcast_batches (...);
```

**FTS5 Tables** (excluded, rebuilt post-restore):
```sql
CREATE VIRTUAL TABLE scraped_articles_fts USING fts5(
    content,
    content='scraped_articles'
);
-- Rebuilt via: INSERT INTO scraped_articles_fts(scraped_articles_fts) VALUES('rebuild')
```

### Backup Data Format

**Parquet Files** (`artifacts/backup/YYYY-MM-DD/`):
```
backup_2024-01-01/
├── watermark.json              # ISO-8601 per-table timestamps
├── scraped_articles.parquet    # Binary columns for embeddings
├── scraped_articles_vec.parquet
├── long_term_memories.parquet
└── long_term_memories_vec.parquet
```

**Watermark Format**:
```json
{
  "scraped_articles": "2024-01-01T12:00:00",
  "scraped_articles_vec": "2024-01-01T12:00:00"
}
```

**Atomic Write Pattern**:
```python
# exporter.py
write_table_batch(chunk_df, table_name):
    temp_path = f"{table_name}.tmp.parquet"
    final_path = f"{table_name}.parquet"
    
    # Validate embeddings if vector table
    if table_name.endswith("_vec"):
        validate_embeddings(chunk_df)
    
    # Write atomically
    pq.write_table(chunk_df, temp_path)
    os.replace(temp_path, final_path)
```

## 8. Dependencies & Integration

### Runtime Dependencies

- **FastAPI**: Web framework
- **SQLite3**: Primary database (WAL mode)
- **PyArrow**: Parquet export/import (v15+)
- **Botasaurus**: Browser automation (v4.x with `._browser._process_pid`)
- **psutil**: Process management for surgical kills
- **python-telegram-bot**: Telegram API (passive usage only)
- **peewee**: ORM (legacy, some usage)
- **aiofiles**: Async file I/O

### Environment Variables

```bash
# API Configuration
API_PORT=8000
API_HOST=0.0.0.0
ARTIFACTS_DIR=./artifacts

# Database
DATABASE_PATH=./data/anything.db
DB_WRITER_QUEUE_MAX=1000

# Browser
CHROME_USER_DATA_DIR=./chrome_profile
CHROME_USER_AGENT=real
CHROME_WINDOW_SIZE=1920,1080
CHROME_WARMUP_TIMEOUT=90

# Backup
BACKUP_ENABLED=true
BACKUP_BATCH_SIZE=500
BACKUP_ONEDRIVE_DIR=
BACKUP_COMPRESSION=zstd

# Telegram (optional, passive only)
TELEGRAM_BOT_TOKEN=

# SoM / Orchestration
SOM_INJECTION_TIMEOUT=60.0
LLM_CONTEXT_CHAR_LIMIT=800000
```

### Integration Points

**External Services**:
- **Telegram**: Passive delivery only via `api/telegram_client.py`
- **Snowflake**: Optional (via `clients/snowflake_client.py`)
- **OneDrive**: Optional backup destination

**Internal Bridges**:
- **Registry → Orchestration**: `REGISTRY.get_som_tools()` provides browser-compatible tools
- **Orchestrator → Tools**: `run_tool_with_orchestrator()` wraps tool execution
- **Daemon → Browser**: `daemon_manager.get_or_create_driver()` provides shared driver
- **Worker → Database**: Single-writer queue via `database.writer`

### Tight Coupling

**Critical Coupling Points**:
1. **SoM Injector + Botasaurus 4.x**: Specifically uses `._browser._process_pid`
2. **Orchestrator + Browser Daemon**: Must share `clear_job_tracking()` lifecycle
3. **Scraper + SoM**: `extraction.py` calls `inject_som()` expecting sequential IDs
4. **Startup + Warmup**: Tier 3 fails entire app if browser warmup fails
5. **Backup + Database**: Writer must be initialized before backup operations
6. **FTS5 + Restore**: Explicit rebuild after master restore, 300s synchronous wait

**Why Coupled**:
- Botasaurus 4.x changed internal attribute names
- SoM architecture prevents thread leaks via surgical kill integration
- Orchestrator manages shared state (ID tracking, context)
- Warmup verifies system health before accepting jobs
- FTS5 derives from masters, cannot be independently restored

## 9. Setup, Build, and Execution

### Clean Setup

**1. Install dependencies**
```bash
cd c:/New folder/AnythingTools
pip install -r requirements.txt
```

**2. Verify pyarrow**
```bash
python -c "import pyarrow; print(pyarrow.__version__)"
# Must be >= 15.0.0
```

**3. Environment**
```bash
# Create .env file
cp .env.example .env  # If exists, edit manually

# Required:
BACKUP_ENABLED=true
BACKUP_BATCH_SIZE=500
CHROME_USER_DATA_DIR=chrome_profile

# Optional:
TELEGRAM_BOT_TOKEN=your_token_here
```

**4. Start API**
```bash
python app.py
# or
uvicorn app:app --host 0.0.0.0 --port 8000
```

**5. Verify Warmup**
```bash
curl http://localhost:8000/api/metrics
# Should show browser_status: "READY" and chrome_pid
```

### Manual Operations

**Trigger Backup Export**:
```bash
curl -X POST http://localhost:8000/api/backup/export \
  -H "Content-Type: application/json" \
  -d '{"mode": "full"}'
```

**Trigger Backup Restore**:
```bash
curl -X POST http://localhost:8000/api/backup/restore \
  -H "Content-Type: application/json" \
  -d '{"watermark": "2024-01-01T00:00:00"}'
```

**Check Job Logs**:
```bash
curl http://localhost:8000/api/jobs/{job_id}
```

**Surgical Kill (emergency)**:
```python
from utils.browser_daemon import daemon_manager
daemon_manager.surgical_kill()
```

## 10. Testing & Validation

### Unit Tests (`tests/`)
```bash
# Backup system
pytest tests/test_backup.py

# Browser E2E
pytest tests/test_browser_e2e.py
```

**Coverage**:
- Backup export/import with PyArrow schema validation
- Browser warmup flow (requires Chrome)
- SoM injection basic flow (mocked driver)

### Manual Verification

**1. SoM Injection**:
```python
from utils.browser_daemon import daemon_manager
from utils.som_utils import inject_som

driver = daemon_manager.get_or_create_driver()
driver.get("https://example.com")
last_id = inject_som(driver, 1)
print(f"Elements tagged: {last_id - 1}")
# Verify badges visible on page
```

**2. Budget Enforcement**:
```python
from bot.orchestrator_core.eviction import BudgetEnforcer

enforcer = BudgetEnforcer(budget=1000)
items = [{"char_count": 500}, {"char_count": 600}]
result = enforcer.enforce(items)
print(len(result))  # Should be 1 (first item evicted)
```

**3. Surgical Kill**:
```python
from utils.browser_daemon import daemon_manager
import psutil

# Start browser
driver = daemon_manager.get_or_create_driver()
print(f"PID: {daemon_manager.pid}")

# Kill and verify
daemon_manager.surgical_kill()
assert daemon_manager.pid is None
# Verify no chrome processes matching profile
```

**4. Telegram Passive**:
```python
from api.telegram_client import TelegramBot

# Should have NO run_orphan_handshake method
assert not hasattr(TelegramBot, 'run_orphan_handshake')

# Can send (if token + chat_id set)
# Cannot handshake
```

**5. Startup Pipeline**:
```bash
python -c "from utils.startup import run_startup; import asyncio; asyncio.run(run_startup())"
# Should complete all 3 tiers without telegram handshake
```

### Gaps (No Coverage)

- **FTS5 rebuild performance**: No test for 300s synchronous wait
- **Multi-user concurrent writes**: Single-writer designed for low concurrency
- **Schema v10 upgrade**: No test for future migrations
- **OneDrive integration**: Optional, not tested
- **Snowflake client**: Optional, not tested
- **LLM budget enforcement**: Orchestrator logic exists but no end-to-end test
- **Watchdog timer edge cases**: Thread timing issues not covered
- **Chrome 4.x compatibility**: Specific to Botasaurus 4.x, no version matrix

## 11. Known Limitations & Non-Goals

### Critical Constraints

- **No concurrent writers**: Single background thread, queue max 1000
- **No selective restore**: All master tables or nothing
- **No FTS backup**: Must rebuild post-restore (300s wait)
- **No continuous backup**: Batch-only, manual or scheduled
- **No telemetry**: Local SQLite only
- **No automatic migration**: Manual reconciliation
- **No backup verification**: No checksums
- **No real-time browser**: Warmup-based, single shared driver
- **No proactive Telegram**: Strict passive sink
- **Timeout kills**: SoM hangs trigger surgical kill, not graceful retry

### Runtime Limits

- **Worker polling interval**: 1 second
- **Callback retries**: 3 attempts with exponential backoff
- **Warmup timeout**: 90 seconds → sys.exit(1)
- **SoM injection timeout**: 60 seconds → surgical kill
- **Restore wait per table**: 120 seconds
- **FTS rebuild wait**: 300 seconds
- **Writer queue max**: 1000 tasks
- **Backup batch size**: 500 rows (10000 ceiling)
- **Context budget**: 800,000 chars (default, configurable)

### Architectural Trade-offs

**Single Browser Instance**:
- ✅ Simpler lifecycle management
- ✅ Lower memory usage
- ❌ Cannot parallelize browser tools
- ❌ One hang affects all browser operations

**Polling Worker**:
- ✅ Simple, reliable
- ✅ No message broker needed
- ❌ 1s latency minimum
- ❌ No event-driven updates

**SQLite WAL**:
- ✅ Concurrent readers
- ✅ Zero-config
- ❌ Single writer
- ❌ No horizontal scaling

**PyArrow Parquet**:
- ✅ Efficient binary storage
- ✅ Columnar compression
- ❌ Very large files can OOM
- ❌ Requires chunked reads

### Explicit Non-Goals

- 🚫 **Real-time streaming**: No websockets, no live updates
- 🚫 **Distributed workers**: Single process only
- 🚫 **Cloud-native**: Designed for local/single-machine
- 🚫 **Multi-tenant isolation**: Single user model
- 🚫 **Selective backup**: Master tables only
- 🚫 **Incremental restore**: All-or-nothing
- 🚫 **Export verification**: No integrity checks
- 🚫 **Continuous integration**: No CI/CD pipelines
- 🚫 **Auto-scaling**: Fixed single-writer model
- 🚫 **GraphQL API**: REST only
- 🚫 **NoSQL backend**: SQLite only
- 🚫 **Browser automation**: Automation via SoM only, no macro recording
- 🚫 **Real-time collaboration**: Single-user focus
- 🚫 **AI training**: Inference only, no model updates
- 🚫 **Proactive notifications**: Telegram passive-only

## 12. Change Sensitivity

### Most Fragile Components

**1. ChromeDaemonManager** (`utils/browser_daemon.py`)
- **Sensitivity**: 🔴 HIGH
- **Why**: Botasaurus 4.x attribute path (`._browser._process_pid`), surgical kill logic, warmup phases
- **Risk**: Browser update breaks PID capture, warmup failure = app crash
- **Change Impact**: 
  - Botasaurus upgrade: Update PID attribute path
  - Chrome update: May require warmup verification update
  - SoM timeout change: Affects thread leak prevention

**2. SoM Injector & Watchdog** (`utils/som_injector.py`)
- **Sensitivity**: 🔴 HIGH
- **Why**: Timeout logic, JS execution paths, batch size tuning
- **Risk**: Infinite loops in JS, incorrect badge IDs, thread leaks
- **Change Impact**:
  - `run_js()` signature: Breaks watchdog wrapper
  - JS DOM traversal: Affects element counting
  - Timeout value: Too short = false positives, too long = delayed failure

**3. Orchestrator Router** (`bot/orchestrator_core/router.py`)
- **Sensitivity**: 🟡 MEDIUM-HIGH
- **Why**: Glues daemon, context, tools, cleanup
- **Risk**: Memory leaks if cleanup fails, context corruption
- **Change Impact**:
  - `finally` block removal = memory leak
  - Context builder API change = tool breakage
  - Budget enforcer bypass = LLM overflow

**4. Single-Writer Queue** (`database/writer.py`)
- **Sensitivity**: 🔴 HIGH
- **Why**: All writes flow through here, queue limit 1000
- **Risk**: Queue overflow = write loss, slow I/O = backup failure
- **Change Impact**:
  - Queue size change: Affects OOM safety
  - Writer thread crash: All writes blocked
  - WAL mode disable: Concurrency break

**5. FTS5 Rebuild** (`database/lifecycle.py`)
- **Sensitivity**: 🟡 MEDIUM
- **Why**: 300s synchronous wait, blocks restore completion
- **Risk**: Long tables hang restore, memory spike
- **Change Impact**:
  - Timeout reduction: May cause partial rebuild
  - Async rebuild: Breaks synchronous assumption

### Tight Coupling

**Coupling Matrix**:
```
Component A          → Component B                | Coupling | Reason
---------------------|----------------------------|----------|----------------------
SoMInjector          → Botasaurus 4.x API         | HIGH     | PID path, run_js
OrchestratorRouter   → ChromeDaemonManager        | HIGH     | surgical_kill, tracking
Tool Runner          → Orchestration              | HIGH     | Context injection
Scraper/Extraction   → SoM Utils                  | HIGH     | inject_som calls
Startup/Tier 3       → Browser Warmup             | HIGH     | sys.exit on failure
Worker/Manager       → Database Writer            | HIGH     | Job updates
Backup/Restore       → Browser Lock               | MEDIUM   | Exclusive access
Telegram/Client      → Config (passive only)      | LOW      | Token optional
```

**Mitigations**:
- Registry abstraction for tools
- Try/except around SoM injection
- Watchdog timer for JS hangs
- Finally block for cleanup
- Retry logic for callbacks

### Easy Extension

**Easy Areas**:
- **Add tables to backup**: Update `MASTER_TABLES`, `TABLE_SCHEMAS`
- **Change backup compression**: `BACKUP_COMPRESSION` config
- **Tune memory**: Decrease `BACKUP_BATCH_SIZE`
- **Add OneDrive**: Set `BACKUP_ONEDRIVE_DIR`
- **Add new tool**: Create `tools/{name}/` with `Skill.py`, add to registry whitelist
- **Add LLM provider**: Update `clients/llm/factory.py`
- **Change SoM timeout**: `SOM_INJECTION_TIMEOUT` env var
- **Add budget rules**: Modify `BudgetEnforcer.enforce()`

**Hard Areas**:
- **Remove pyarrow**: Rewrite all backup I/O
- **Async backup**: `aiofiles` + async DB (writer is sync)
- **Schema v10**: Update DDL + migration script
- **API v2**: New endpoints, backward compatibility
- **Prompt migration**: Update imports, validate LLM changes
- **Multi-browser**: Rewrite singleton daemon manager
- **Worker polling → event-driven**: New architecture entirely
- **Proactive Telegram**: Would require architectural rule violation