# app.py
"""
AnythingTools FastAPI application entrypoint with lifespan hooks:
- mounts artifacts/
- runs zombie-chrome cleanup
- starts DB writer thread
- applies SQL migrations found under database/migrations/
python -m uvicorn app:app --reload --port 8000
"""

from fastapi import FastAPI, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.security.api_key import APIKeyHeader
from fastapi import Security, HTTPException
from contextlib import asynccontextmanager
import logging
from dataclasses import dataclass, field
from typing import List
import os
import asyncio
from pathlib import Path

import config as config_module



try:
    import psutil
except Exception:
    psutil = None

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=True)

async def verify_api_key(api_key: str = Security(api_key_header)):
    """
    Dependency injected into all /api/ routes.
    Compares the header against the secret loaded in config.py.
    """
    if api_key != config_module.API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid API Key. Unauthorized access."
        )
    return api_key

# Lazy imports for DB writer and migration runner
from database.connection import DatabaseManager
from database.writer import (
    start_writer,
    shutdown_writer,
    enqueue_write,
    enqueue_execscript,
    wait_for_writes,
)
from utils.logger.core import get_dual_logger
from tools.registry import REGISTRY

# MIGRATIONS_DIR is now managed by database.migrations; removed from app.py

log = get_dual_logger(__name__)


@dataclass
class StartupHealth:
    ok: bool = True
    failures: List[str] = field(default_factory=list)

    def require(self, step_name: str, exc: Exception) -> None:
        self.ok = False
        self.failures.append(step_name)
        log.dual_log(
            tag="Sys:Startup",
            message=f"Critical startup step failed: {step_name}: {exc}",
            level="CRITICAL",
            exc_info=exc,
        )
        raise RuntimeError(f"Startup step '{step_name}' failed: {exc}") from exc


async def validate_vec0_extension() -> None:
    """Best-effort runtime validation of the sqlite_vec / vec0 extension.

    This does NOT abort startup; it logs a warning and continues in compatibility
    mode when the native extension isn't present. The historical main() used
    a hard exit, but AnythingTools prefers to be tolerant in developer environments.
    """
    try:
        from database.connection import SQLITE_VEC_AVAILABLE
    except Exception:
        SQLITE_VEC_AVAILABLE = False

    if not SQLITE_VEC_AVAILABLE:
        log.dual_log(
            tag="Sys:Vec0",
            message="sqlite_vec/vec0 extension not available; running in compatibility mode.",
            level="WARNING",
        )
        return

    # Verify vec0 can be loaded by a temporary connection
    try:
        conn = DatabaseManager.get_read_connection()
        cursor = conn.execute("SELECT vec_version();")
        version = cursor.fetchone()
        log.dual_log(
            tag="Sys:Vec0",
            message=f"sqlite_vec/vec0 extension verified: {version}",
            level="INFO",
        )
    except Exception:
        log.dual_log(
            tag="Sys:Vec0",
            message="Could not invoke vec_version(). Extension might be missing or incompatible.",
            level="WARNING",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup:
        1) Mount artifacts/ static directory
        2) Performance tune SQLite pragmas
        3) Start DB writer thread
        4) Run migrations
        5) Validate vec0 extension (warnings only)
        6) Cleanup orphaned Chrome processes
        7) Ensure chrome_download/ exists
        8) Cleanup orphaned Parquet temp files
    
    Shutdown:
        1) Drain and shutdown DB writer
    """
    health = StartupHealth()

    # === Step 1: Mount artifacts/ static directory
    artifacts_dir = Path("artifacts")
    artifacts_dir.mkdir(exist_ok=True)
    # Mount at /api/artifacts/ and /artifacts/
    app.mount("/api/artifacts", StaticFiles(directory=str(artifacts_dir)), name="artifacts")
    app.mount("/artifacts", StaticFiles(directory=str(artifacts_dir)), name="artifacts_public")

    # === Step 1.2: Performance tune SQLite pragmas
    pragmas = [
        "PRAGMA journal_mode=WAL;",
        "PRAGMA synchronous=NORMAL;",
        "PRAGMA cache_size=-64000;",
        "PRAGMA temp_store=MEMORY;",
        "PRAGMA foreign_keys=ON;",
        "PRAGMA mmap_size=268435456;",
    ]
    try:
        conn = DatabaseManager.get_read_connection()
        for p in pragmas:
            try:
                conn.execute(p)
            except Exception:
                pass
        log.dual_log(tag="DB:Init", message="SQLite pragmas tuned", level="INFO")
    except Exception:
        log.dual_log(tag="DB:Init", message="Failed to tune SQLite pragmas", level="WARNING")

    # === Step 1.3: Start DB writer thread
    try:
        start_writer()
        log.dual_log(tag="DB:Writer", message="Writer thread started", level="INFO")
    except Exception as e:
        health.require("db_writer", e)

    # === Step 1.4: Zombie-chrome cleanup
    zombie_count = 0
    try:
        if psutil:
            for p in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    name = p.info["name"] or ""
                    cmdline = " ".join(p.info["cmdline"] or [])
                    if "chrome" in name.lower() or "chromium" in name.lower():
                        if p.status() == "zombie" or "--headless" in cmdline:
                            zombie_count += 1
                            p.kill()
                        elif os.name != "nt" and "DISPLAY" not in os.environ and "--headless" not in cmdline:
                            zombie_count += 1
                            p.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                except Exception:
                    pass
            if zombie_count > 0:
                log.dual_log(tag="Browser:Cleanup", message=f"Cleaned up {zombie_count} zombie chrome processes", level="INFO")
    except Exception:
        pass

    try:
        os.makedirs("chrome_download", exist_ok=True)
    except Exception:
        pass

    try:
        from tools.backup.config import BackupConfig
        bak_cfg = BackupConfig.from_global_config()
        if bak_cfg.enabled and bak_cfg.backup_dir.exists():
            for p in bak_cfg.backup_dir.rglob("*.tmp.parquet"):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception as e:
        log.dual_log(tag="Sys:Startup", message=f"Failed cleaning backup temp files: {e}", level="WARNING")

    # === Step 2: Database lifecycle (CRITICAL) ===
    from database.lifecycle import run_database_lifecycle
    await run_database_lifecycle()

    # === Step 3: Tool registry (CRITICAL) ===
    try:
        REGISTRY.load_all()
        loaded = len(REGISTRY._tools)
        if loaded == 0:
            raise RuntimeError("Registry loaded 0 tools.")
        log.dual_log(tag="Tools:Registry", message=f"Registry loaded: {loaded} tools", level="INFO")
    except Exception as e:
        health.require("tool_registry", e)

    # === Step 4: Browser driver warmup (CRITICAL) ===
    try:
        from utils.browser_daemon import get_or_create_driver
        from utils.browser_lock import browser_lock
        
        def _warmup_browser():
            browser_lock.acquire()
            try:
                driver = get_or_create_driver()
                driver.run_js("return 1;")
            finally:
                browser_lock.safe_release()
                
        await asyncio.wait_for(asyncio.to_thread(_warmup_browser), timeout=30.0)
        log.dual_log(tag="Browser:Warmup", message="Driver responsive", level="INFO")
    except Exception as e:
        health.require("browser_warmup", e)

    # === Step 5: Validate vec0 extension ===
    try:
        await validate_vec0_extension()
    except Exception as e:
        log.dual_log(tag="Sys:Vec0", message=f"Vec0 validation skipped: {e}", level="WARNING")

    if not health.ok:
        raise RuntimeError(f"Startup incomplete: {health.failures}")

    log.dual_log(tag="Sys:Startup", message="Lifespan startup completed", level="INFO")

    yield

    # === Shutdown ===
    try:
        await wait_for_writes()
        shutdown_writer()
        log.dual_log(tag="DB:Writer", message="Writer thread stopped and writes flushed", level="INFO")
    except Exception as e:
        log.dual_log(tag="DB:Writer", message=f"Error during writer shutdown: {e}", level="ERROR")


# Create FastAPI app with lifespan
app = FastAPI(lifespan=lifespan, title="AnythingTools API", version="1.0.0")

# === Middlewares ===
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, lock this down
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

# === Mount routers ===
from api.routes import router as api_router
app.include_router(api_router, prefix="/api", dependencies=[Depends(verify_api_key)])

# === Public routes (no API key required) ===
from api.routes import router as public_router
# Public router is the same but without auth - for now, only /manifest is public
from fastapi import APIRouter

public_router_no_auth = APIRouter()

@public_router_no_auth.get("/manifest")
async def public_manifest():
    from tools.registry import REGISTRY
    REGISTRY.load_all()
    return {"tools": REGISTRY.schema_list()}

app.include_router(public_router_no_auth, prefix="/api")

@app.get("/", include_in_schema=False)
async def root():
    return {"message": "AnythingTools API", "version": "1.0.0", "docs": "/docs"}
