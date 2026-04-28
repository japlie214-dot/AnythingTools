# AnythingTools - Browser Automation and Observability System

## 1. Project Overview

**Operational Purpose:** AnythingTools is a production-grade browser automation system that integrates with Botasaurus to provide Set-of-Marks (SoM) observation extraction for AI agents. It operates as a FastAPI service that orchestrates browser tasks, extracts structured DOM observations, and maintains browser state across tool executions.

**Problems Solved:**
- **DOM Observation for LLMs**: Attaches persistent, non-colliding identifiers (`bid_N`) to interactive elements without modifying DOM structure permanently
- **Browser State Management**: Maintains single-tab policy and surgical process control with zombie Chrome cleanup
- **Heuristic-Based Element Detection**: Identifies clickable/interactive elements using geometry, computed styles, and event handlers without O(N) reflow thrashing
- **Tool Execution Orchestration**: Provides guaranteed cleanup (`finally` block) for temporary DOM modifications across tool runs

**Explicit Non-Goals:**
- **No Async JavaScript Execution**: All JS runs via blocking `run_js()` calls; no Promise-based or event-driven script execution
- **No Dynamic Configuration Reload**: Config values are loaded once at startup (from environment variables)
- **No Headless/Stealth Mode**: Browser runs in visible mode; stealth/anti-detection features are not implemented
- **No Multi-User Support**: Designed for single-user local deployment; no session isolation or authentication beyond basic API key

## 2. High-Level Architecture

### System Components

```
┌─────────────────────────────────────────────────────────────────┐
│                        FastAPI Entry Point (app.py)              │
│  - Lifespan: Startup/Shutdown hooks                              │
│  - API Key Authentication                                        │
│  - Mounts /api routes with auth                                  │
└─────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Bot Orchestrator (router.py)                  │
│  - SoMContextBuilder initialization                              │
│  - Injects SoM markers before tool execution                     │
│  - Guaranteed post_extract() cleanup in finally block            │
│  - Surgical kill on MarkingError                                 │
└─────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│              Browser Daemon Manager (browser_daemon.py)          │
│  - Chrome process lifecycle management                           │
│  - Tab Management Test (google.com open/close)                   │
│  - Space Jam 1996 warmup validation                              │
│  - Surgical kill via psutil for zombie cleanup                   │
└─────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│              Botasaurus Driver (External Dependency)             │
│  - Chrome browser automation via CDP                             │
│  - Synchronous run_js() execution                                │
└─────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│              JavaScript Assets (frame_mark_elements.js)          │
│  - 0-indexed bid counter: bid_0, bid_1, ...                      │
│  - Heuristic SoM detection (clickable, pointer, geometry)        │
│  - Shadow DOM traversal support                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow: Tool Execution with SoM

```
1. Tool Request Received (e.g., scraper with URL)
   │
2. Orchestrator creates SoMContextBuilder
   │
3. Browser Daemon ensures driver is alive
   │
4. wait_for_dom_stability(driver)  // Wait for DOM to stabilize
   │
5. inject_som(driver) → BotasaurusObservationAdapter.pre_extract()
   │   ├─ Runs frame_mark_elements.js (blocking)
   │   ├─ Returns: {marked_count: N, som_count: M, last_bid: "bid_(N-1)"}
   │   └─ Tracks range: (0, N-2) in id_tracking['main']
   │
6. Tool execution (with context: som_context, element_hints)
   │   ├─ Tool can reference bid_0, bid_1, etc.
   │   └─ Scraper engagement: random.randint(0, N-2) → scroll into view
   │
7. Finally block executes (guaranteed):
   │   ├─ BotasaurusObservationAdapter.post_extract()
   │   │   └─ Runs frame_unmark_elements.js (removes data-ai-id, visibility, set_of_marks)
   │   └─ daemon_manager.clear_job_tracking()  // Cleans id_tracking dict
   │
8. Response returned
```

### Control Flow & Lifecycle

**Runtime Model:** Event-driven via HTTP requests; each request spawns isolated execution context with browser lifecycle management.

**Execution Model:** Synchronous within tool execution (no async/await in core observation logic). Browser daemon maintains single long-lived Chrome process with surgical restart capabilities.

**State Transitions:**
- `INITIALIZING` → `READY` (after warmup)
- `READY` → `DEGRADED` (after failures)
- `READY` → `CRITICAL_FAILURE` (on MarkingError) → `surgical_kill()` → `INITIALIZING`

## 3. Repository Structure

```
AnythingTools/
│
├── app.py                              # FastAPI entrypoint with lifespan hooks
├── config.py                           # Environment-based configuration
├── README.md                           # This file
├── requirements.txt                    # Python dependencies
│
├── api/                                # HTTP endpoints and auth
│   ├── routes.py                      # Main router (auth-protected)
│   ├── schemas.py                     # Pydantic models
│   ├── telegram_client.py             # Telegram API wrapper
│   └── telegram_notifier.py           # Message publishing
│
├── bot/                                # Core orchestration logic
│   ├── core/
│   │   └── constants.py               # App constants
│   ├── engine/
│   │   ├── worker.py                  # Job worker manager
│   │   └── tool_runner.py             # Tool execution runner
│   └── orchestrator_core/
│       ├── router.py                  # SoM-aware tool router (CRITICAL)
│       ├── context.py                 # SoMContextBuilder
│       └── eviction.py                # Budget enforcement
│
├── tools/                              # Tool implementations
│   ├── base.py                        # Base tool classes
│   ├── registry.py                    # Tool registry (dynamic loading)
│   ├── batch_reader/                  # Batch reading tool
│   ├── draft_editor/                  # Draft editing tool
│   ├── publisher/                     # Publishing tool
│   └── scraper/                       # Web scraper (SoM-enabled)
│       ├── extraction.py              # URL extraction with random scroll
│       ├── browser.py                 # Hybrid HTML extraction
│       ├── paywall.py                 # Paywall detection
│       ├── task.py                    # Scraping task orchestration
│       └── tool.py                    # CLI interface
│
├── utils/                              # Core utilities
│   ├── javascript/                    # JS assets for Dom injection
│   │   ├── frame_mark_elements.js     # SoM injection (0-indexed)
│   │   └── frame_unmark_elements.js   # Cleanup script
│   │
│   ├── logger/                        # Dual logger (console + structured)
│   │   ├── core.py
│   │   ├── formatters.py
│   │   └── handlers.py
│   │
│   ├── startup/                       # Lifespan initialization
│   │   ├── browser.py                 # Browser warmup (Space Jam)
│   │   ├── database.py                # DB migrations
│   │   └── core.py                    # Coordinated startup
│   │
│   ├── observation_adapter.py         # SoM adapter (synchronous, MarkingError)
│   ├── browser_daemon.py              # Chrome lifecycle manager
│   ├── browser_utils.py               # safe_google_get with logging
│   ├── action_mapper.py               # bid → CSS selector (str|int support)
│   ├── som_utils.py                   # inject_som, reinject_all, etc.
│   ├── browser_lock.py                # File-based Chrome mutex
│   ├── text_processing.py             # HTML cleaning for LLMs
│   ├── vision_utils.py                # Screenshot capture
│   └── ...
│
├── database/                           # Persistence layer
│   ├── schemas/                       # Peewee ORM models
│   ├── connection.py                  # DB connection management
│   ├── writer.py                      # Async write buffer
│   ├── job_queue.py                   # Job queue with status tracking
│   └── ...
│
├── clients/                            # External service clients
│   ├── llm/                           # LLM client factory (Azure/Chutes)
│   │   ├── factory.py
│   │   └── providers/
│   └── snowflake_client.py            # Snowflake integration
│
├── deprecated/                         # Legacy code (evidences of evolution)
│   ├── bot/core/                      # Old agent/modes implementations
│   ├── tools/                         # Deprecated tools (finance, search, etc.)
│   └── ...                            # See "Changes" section
│
└── tests/                              # Test files
    ├── test_backup.py
    └── test_browser_e2e.py            # End-to-end browser tests
```

**Notable Structural Choices:**
- **`deprecated/` directory**: Contains evidence of prior architecture (monolithic agent, finance tools). Actively unused but retained for historical analysis.
- **`javascript/` subdirectory**: JS assets colocated with Python code for clarity; loaded via `pkgutil.get_data()` or file fallback.
- **`orchestrator_core/`**: Decoupled from main bot logic to isolate SoM concerns; router.py enforces cleanup.

## 4. Core Concepts & Domain Model

### Key Abstractions

**1. Set-of-Marks (SoM)**
- **Definition**: Temporary attributes (`data-ai-id`, `browsergym_set_of_marks`, `browsergym_visibility_ratio`) attached to DOM elements to provide AI agents with clickable identifiers.
- **Invariant**: Attributes must be removed after tool execution (observed via `post_extract()` in `finally` block).
- **Namespace**: Flat 0-indexed `bid_N` format (`bid_0`, `bid_1`, ... `bid_(n-1)`). No hierarchical IDs.

**2. Bidirectional Mapping (Action Mapper)**
- **Input Types**: String (`"bid_0"`) or Integer (`0`)
- **Output**: CSS Selector `[data-ai-id="bid_0"]`
- **Purpose**: Decouples LLM (which thinks in numeric IDs) from implementation (which uses string selectors).

**3. MarkingError**
- **Type**: Custom exception class in `observation_adapter.py`
- **Usage**: Raised when `pre_extract()` fails and `lenient=False`
- **Handler**: Router catches and triggers `daemon_manager.surgical_kill()`

**4. SoMContextBuilder**
- **Location**: `bot/orchestrator_core/context.py`
- **Function**: Wraps `inject_som()` result into tool execution context
- **Delivers**: `som_context` dict and `element_hints` to tools

### Implicit Rules & Assumptions

**Domain Rules:**
1. **Bid uniqueness**: JavaScript guarantees no duplicate `bid_N` via `allBids` Set
2. **Zero-trust cleanup**: `post_extract()` runs even if tool crashes (finally block)
3. **Single-tab invariant**: `enforce_single_tab()` strips `target="_blank"` and overrides `window.open`
4. **Geometry-first detection**: SoM uses `getBoundingClientRect()` + computed styles; **explicitly avoids** `document.elementFromPoint()` (causes reflow)
5. **Blocking execution**: All JS runs synchronously; no async/await in observation layer

**Technical Constraints:**
- **Chrome only**: Botasaurus dependency; no Firefox/WebKit support
- **Windows path assumption**: `CHROME_USER_DATA_DIR` uses Windows-style paths
- **Synchronous**: No concurrent browser operations; one tool at a time
- **Process-level isolation**: Zombie Chrome cleanup via `psutil.process_iter()` (not OS-agnostic)

### Terminology

| Term | Definition | Evidence |
|------|------------|----------|
| **Bid** | String ID like `bid_0` injected by JS | `frame_mark_elements.js:85` |
| **SoM** | Set-of-Marks: elements with `browsergym_set_of_marks="1"` | `frame_mark_elements.js:92-125` |
| **Surgical Kill** | Process-level Chrome termination via psutil | `browser_daemon.py:66-101` |
| **Watchdog** | Threading.Timer for timeout (documentation only; run_js is blocking) | `observation_adapter.py:40-61` |
| **Hybrid HTML** | Greedy leaf-node extraction preserving SoM attrs | `browser_utils.py:46-79` |

## 5. Detailed Behavior

### Normal Execution: Scraping a URL

```
Request: POST /api/tools/scraper
Body: { "url": "https://example.com/article" }

Flow:
1. app.py::lifespan() ensures browser_daemon is READY
2. bot/orchestrator_core/router.py::run()
   ├─ context_builder.initialize("scraper", {url})
   ├─ driver = daemon_manager.get_or_create_driver()
   ├─ wait_for_dom_stability(driver)  // Polls DOM element count
   ├─ inject_som(driver)
   │   └─ BotasaurusObservationAdapter.pre_extract()
   │       ├─ run_js(frame_mark_elements.js, {"bid_attr": "data-ai-id"})
   │       └─ Returns: {marked_count: 42, som_count: 3, last_bid: "bid_41"}
   ├─ context_builder.inject_som_markers((0, 39))
   └─ tool_executor(scraper, args, som_context=..., element_hints=...)
       └─ tools/scraper/task.py::process_article()
           ├─ safe_google_get(driver, url)  // Logs: Browser:Navigate url
           ├─ extract_hybrid_html(driver)  // Preserves data-ai-id
           ├─ Scroll engagement: random.randint(0, 39) → scrollIntoView
           └─ LLM extraction with SoM context
   └─ FINALLY: post_extract() + clear_job_tracking()
Response: { "status": "SUCCESS", "data": { ... } }
```

### Edge Cases & Error Handling

**Case 1: MarkingError during warmup**
- **Detection**: `reinject_all()` raises `MarkingError`
- **Action**: 
  ```python
  # browser_daemon.py:222-226
  if isinstance(e, MarkingError):
      log.dual_log(..., level="CRITICAL")
      self.surgical_kill()
      self._status = BrowserStatus.CRITICAL_FAILURE
      raise RuntimeError("SoM Injection caused infinite loop. Chrome killed.")
  ```

**Case 2: Tab opens during execution**
- **Detection**: Single-tab policy violation
- **Action**: `enforce_single_tab()` patches `window.open` and strips `target="_blank"` immediately after navigation

**Case 3: Chrome hang on run_js**
- **Limitation**: Watchdog exists but cannot interrupt blocking call; relies on Botasaurus driver timeout or external surgical kill
- **Evidence**: Comments in `observation_adapter.py:97-98` acknowledge blocking nature

**Case 4: Post-extract cleanup failure**
- **Handling**: Silent failure, warning logged only
  ```python
  # observation_adapter.py:131-133
  except Exception:
      pass  // Never raises
  ```

**Case 5: Video detection false positives**
- **Mitigation**: Checks actual platform strings in iframe src
  ```python
  # extraction.py:184
  video_platforms = ["youtube.com/embed", "youtu.be", "vimeo.com", "dailymotion.com"]
  ```

### Configuration Paths

**Primary**: Environment variables via `.env` loaded in `config.py`

**Critical SoM Configs** (used in adapter/JS):
- `BROWSER_SOM_TAGS_TO_MARK` → JS `TAGS_TO_MARK` (default: "standard_html")
- `BROWSER_SOM_SCALE_FACTOR` → Adapter scales bbox coordinates (default: 1.0)
- `BROWSER_SOM_HTML_CHAR_BUDGET` → Limits extracted HTML size (default: 20000)

**Other Key Configs**:
- `CHROME_USER_DATA_DIR` → Chrome profile location
- `API_KEY` → FastAPI authentication
- `ANYTHINGLLM_*` → External LLM integration

## 6. Public Interfaces

### HTTP API

**Authenticated Endpoints (`/api` prefix)**
- **Tool Execution**: `POST /api/tools/{tool_name}`
  - **Auth**: `X-API-Key` header required
  - **Body**: Tool-specific args
  - **Response**: Tool result + SoM context (if browser used)

**Public Endpoints**
- **Manifest**: `GET /api/manifest` (no auth)
  - Returns registry of all available tools

### Python Callables (Internal)

**Primary Orchestration:**
- `bot/orchestrator_core/router.py::OrchestratorRouter.run()`
  - **Parameters**: `tool_name`, `tool_args`, `tool_executor`, `browser_daemon`
  - **Returns**: `ToolResult`
  - **Side Effects**: Injects/removes SoM attributes, manages browser lifecycle

**Browser Control:**
- `utils/browser_daemon.py::daemon_manager.get_or_create_driver()`
  - **Returns**: Botasaurus `Driver` instance
  - **Lifecycle**: Long-lived, auto-reinitialized if dead

**SoM Injection:**
- `utils/som_utils.py::inject_som(driver, start_id=1) → int`
  - **Returns**: `marked_count + 1` (last ID + 1)
  - **Behavior**: Delegates to `BotasaurusObservationAdapter`

**Action Resolution:**
- `utils/action_mapper.py::click(driver, bid: str | int)`
- `utils/action_mapper.py::fill(driver, bid: str | int, value: str)`
- `utils/action_mapper.py::hover(driver, bid: str | int)`

### Tool Entry Points

**Scraper:**
- `tools/scraper/tool.py` (CLI via `python -m tools.scraper.tool`)
- `tools/scraper/task.py::process_article()` (programmatic)

**Batch Reader:**
- `tools/batch_reader/tool.py`

**Publisher:**
- `tools/publisher/tool.py`

### Expected Inputs/Outputs

| Interface | Input Type | Output Type | Constraints |
|-----------|------------|-------------|-------------|
| `inject_som()` | `Driver` | `int` | Must be called after `wait_for_dom_stability()` |
| `post_extract()` | `Driver` | `None` | Run in `finally`; never raises |
| `action_mapper.click()` | `Driver`, `str\|int` | `None` | Element must exist; raises `ValueError` if not |
| `safe_google_get()` | `Driver`, `URL` | `None` | Logs URL explicitly via `Browser:Navigate` |

## 7. State, Persistence, and Data

### In-Memory State

**Browser Daemon:**
- `_driver`: Long-running Botasaurus Driver instance
- `_id_tracking`: Dict mapping `'main'` → `(start, end)` tuple of bid range
- `_action_log`: `deque(maxlen=50)` of recent operations
- `_status`: `BrowserStatus` enum

**Orchestrator (per-request):**
- `SoMContextBuilder`: Temporary context built for single tool execution
- **Cleared in finally**: `daemon_manager.clear_job_tracking()`

### Persistent State

**Database (SQLite/Peewee):**
- **Schemas**:
  - `Job` (jobs.py): Tracks scraper tasks, status, metadata
  - `TokenUsage` (token.py): LLM token counts
  - `FinanceData` (finance.py): Financial metrics (legacy)
  - `PDFMetadata` (pdf.py): PDF processing state
  - `VectorEmbedding` (vector.py): Semantic search embeddings

**Files:**
- **Chrome Profile**: `CHROME_USER_DATA_DIR` (user-managed)
- **Artifacts**: `data/temp/` (screenshots, logs)
- **Backups**: Parquet files in `BACKUP_ONEDRIVE_DIR` (if enabled)

### Data Lifecycle

**Scraper Job:**
```
enqueue_job() → RUNNING (metadata cached) → SUCCESS/FAILED
   ↓
metadata persisted to DB after validation & summary
   ↓
on resume: check metadata → skip if validation_passed=True
```

**SoM Attributes:**
```
pre_extract() → Injected (visible during tool execution)
   ↓
finally block → post_extract() → Removed
   ↓
Effective lifecycle: Single request only
```

### Migration & Cleanup

**Schema Migrations:**
- **Location**: `database/schemas/` (versioned via Peewee schema introspection)
- **Control**: `SUMANAL_ALLOW_SCHEMA_RESET=1` environment variable required for destructive resets

**Cleanup Behavior:**
- **On Shutdown**: `app.py` drains jobs → kills browser → closes DB writer
- **On Startup**: Removes orphan Chrome processes (surgical kill), runs DB migrations
- **Manual**: `daemon_manager.surgical_kill()` forces Chrome termination

## 8. Dependencies & Integration

### External Dependencies

| Package | Usage | Reason |
|---------|-------|--------|
| **botasaurus** | Browser automation, CDP | Direct wrapper for Chrome DevTools Protocol |
| **fastapi** | HTTP server, routing | Async request handling with lifespan hooks |
| **peewee** | ORM for SQLite | Lightweight DB for job tracking |
| **psutil** | Process management | Surgical Chrome kill (pid-based) |
| **beautifulsoup4** | HTML parsing | Hybrid extraction, paywall detection |
| **aiofiles** | Async file I/O | Non-blocking file operations |
| **snowflake-connector** | Warehouse integration | Enterprise data persistence |
| **openai** / **azure** | LLM clients | Tool result processing |
| **pydantic** | Config/validation | Type-safe config loading |

### Environment/Assumptions

**Platform**: Windows 10/11 (evidenced by `psutil`, `cmd.exe` commands in `.bat` scripts)

**Chrome**: Must be installed with user data directory writable

**Python**: 3.11+ (type hints, union syntax `str | int`)

**Network**: Access to:
- `spacejam.com/1996/` (warmup validation)
- `google.com` (tab management test)
- Target scraping URLs

### Coupling Points

**Tight Coupling:**
- `frame_mark_elements.js` ↔ `observation_adapter.py` (contract on return value shape)
- `router.py` ↔ `browser_daemon.py` (exception handling `MarkingError` → surgical kill)
- `action_mapper.py` ↔ Tool execution (expects `bid_N` format)

**Looser Coupling:**
- Tools ↔ LLM providers (configurable via factory)
- Database ↔ Persistence (can be disabled if migrations fail gracefully)

### External Service Assumptions

**Azure OpenAI**:
- Assumes `AZURE_DEPLOYMENT` exists; defaults to `gpt-5.4-mini`

**Chutes**:
- Fallback provider; requires `CHUTES_API_TOKEN`

**Snowflake**:
- Optional; requires private key file at `SNOWFLAKE_PRIVATE_KEY_PATH`

**Telegram**:
- Optional push notifications; requires `TELEGRAM_BOT_TOKEN` + user handshake

## 9. Setup, Build, and Execution

### Prerequisites

```bash
# Windows only
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Environment Configuration

Create `.env` file:
```bash
# Required
API_KEY=your-secret-key-here
CHROME_USER_DATA_DIR=C:\path\to\chrome\profile

# SoM Configuration (optional, defaults provided)
BROWSER_SOM_TAGS_TO_MARK=standard_html
BROWSER_SOM_SCALE_FACTOR=1.0
BROWSER_SOM_HTML_CHAR_BUDGET=20000

# LLM (choose one)
AZURE_OPENAI_KEY=...
AZURE_OPENAI_ENDPOINT=...
AZURE_DEPLOYMENT=gpt-5.4-mini

# OR
CHUTES_API_TOKEN=...
CHUTES_MODEL=meta-llama/Llama-3.3-70B-Instruct

# Optional
TELEGRAM_BOT_TOKEN=...
TELEGRAM_USER_ID=...
SNOWFLAKE_ACCOUNT=...
```

### Running the Application

**Development (Hot Reload):**
```bash
python -m uvicorn app:app --reload --port 8000
```

**Production:**
```bash
# No reload for stability
python -m uvicorn app:app --port 8000
```

**Docker** (not provided in repo; would need custom Dockerfile exposing port 8000 and mounting Chrome profile)

### Tool Execution (Scraper Example)

**CLI:**
```bash
python -m tools.scraper.tool --url https://example.com --output result.json
```

**Programmatic:**
```python
from bot.orchestrator_core.router import OrchestratorRouter
from tools.scraper.task import process_article

router = OrchestratorRouter(job_id="test-001")
result = await router.run(
    tool_name="scraper",
    tool_args={"url": "https://example.com"},
    tool_executor=process_article,
    browser_daemon=daemon_manager
)
```

### Build Processes

**None**: This is pure Python code; no compilation step. Dependencies are installed via pip.

**Just-In-Time Building**: Tool registry loads classes dynamically via entry points or importlib.

## 10. Testing & Validation

### Test Structure

```
tests/
├── test_backup.py          # Parquet backup integration
└── test_browser_e2e.py     # End-to-end browser tests
```

### Running Tests

```bash
# All tests
python -m pytest tests/

# Browser only
python -m pytest tests/test_browser_e2e.py
```

### Test Coverage Gaps

**Observable Gaps** (based on file list):
- **No unit tests** for `observation_adapter.py` (tested only via integration)
- **No direct tests** for `frame_mark_elements.js` (requires browser runtime)
- **No tests** for `action_mapper.py` (presumably manual validation)
- **No tests** for `router.py` (orchestrator logic untested in isolation)

**Existing Coverage**:
- `test_backup.py`: Tests database backup workflow
- `test_browser_e2e.py`: Likely tests scraper with real browser (inferred from name)

### Test Interpretation

**Success Criteria**: 
- E2E tests pass → browser automation, SoM injection, extraction working end-to-end
- Backup tests pass → DB state persistence validated

## 11. Known Limitations & Non-Goals

### Hard-Coded Constraints

1. **Blocking Execution**: 
   - `run_js()` is synchronous; JS hangs block main thread
   - **Mitigation**: Watchdog exists but cannot preempt; relies on external `surgical_kill()`

2. **Single-Tab Policy**:
   - Hard-coded in `enforce_single_tab()` 
   - **Impact**: Cannot test multi-tab workflows; popups blocked at runtime

3. **Windows-Centric**:
   - Path separators, `psutil` assumptions
   - **Impact**: Likely fails on Linux/macOS without modification

4. **No Headless Mode**:
   - Chrome always launches visibly
   - **Impact**: Not suitable for CI/CD or server environments without X11

5. **Static Configuration**:
   - Config loaded once at startup
   - **Impact**: Requires restart to change SoM parameters or LLM providers

### Missing Features (Despite Proxies)

- **Async JS Execution**: Not implemented despite `aiofiles` dependency
- **Migrations UI**: No CLI or admin interface for schema resets
- **Metrics/Telemetry**: `TELEMETRY_DRY_RUN` exists but no actual metrics collection
- **Session Isolation**: Single user context; no multi-tenant support

### Technical Debt (Visible in Code)

1. **Legacy Import in Scraper** (FIXED in PLAN-02):
   - Evidence: Old reference to `utils.som_injector` now updated to `observation_adapter`

2. **Unused `deprecated/` Code**:
   - Contains finance, polymarket, vector_memory tools
   - **Impact**: Maintenance burden, potential confusion for new contributors

3. **Comment-Only Watchdog**:
   - `WatchdogTimer` is documented but functionally limited due to blocking `run_js()`
   - **Risk**: False sense of security about timeout protection

4. **Vague "Standard HTML"**:
   - `TAGS_TO_MARK = "standard_html"` is a magic string with no specification
   - **Impact**: Unclear what elements are marked without reading JS

## 12. Change Sensitivity

### Most Fragile Components

**1. `bot/orchestrator_core/router.py`**
- **Why**: Central orchestration; modifying `post_extract()` or exception handling breaks cleanup guarantees
- **Risk**: Memory leaks, zombie Chrome processes if cleanup removed
- **Safe Changes**: Adding logging, modifying context injection

**2. `utils/javascript/frame_mark_elements.js`**
- **Why**: Contract with `observation_adapter.py` on return value; changes affect all downstream tools
- **Risk**: SoM detection failures, LLM confusion over bid format
- **Safe Changes**: Adding new detection heuristics (must preserve `bid_N` format)

**3. `utils/browser_daemon.py::reinject_all()`**
- **Why**: Range calculation `(0, last_id - 2)` must match JS 0-indexing
- **Risk**: Off-by-one errors → LLM references non-existent `bid_N`
- **Safe Changes**: None; range logic is critical

**4. `utils/som_utils.py::inject_som()`**
- **Why**: Delegates to adapter; signature affects all callers
- **Risk**: Breaks backward compatibility with legacy tools
- **Safe Changes**: None

### Easiest to Extend

**1. Tools (New Implementations)**
- **Pattern**: Inherit from `tools.base.Tool`, register via `tools.registry`
- **Mechanism**: Dynamic loading, no core changes needed

**2. LLM Providers**
- **Pattern**: Add new class to `clients/llm/providers/`, update factory
- **Mechanism**: Config-driven selection

**3. Logging Formats**
- **Pattern**: Modify `utils/logger/formatters.py`
- **Mechanism**: Decoupled from business logic

### Hardest to Change

**1. SoM Namespace (bid_N to something else)**
- **Impact**: Requires changes to JS, Python adapter, router, action_mapper, all tools
- **Scope**: Cross-cutting; would break existing scraper logic

**2. Botasaurus Dependency**
- **Impact**: All browser operations tied to Botasaurus API
- **Replacement**: Would require rewriting `browser_daemon.py`, `observation_adapter.py`, `som_utils.py`

**3. Synchronous Architecture**
- **Impact**: Core assumption in `observation_adapter.py` and `router.py`
- **Effort**: Would require async/await proliferation, rethinking watchdog timers