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
from fastapi.security.api_key import APIKeyHeader
from fastapi import Security, HTTPException
from contextlib import asynccontextmanager
import logging

import config as config_module

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

from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from utils.startup import run_startup
        await run_startup(app)
        log.dual_log(tag="App:Lifespan", message="Startup completed successfully", level="INFO")

        yield
    finally:
        # === Shutdown (ordered: Stop Polling -> Cancel -> Drain -> Browser Driver → DB Writer) ===
        try:
            from bot.engine.worker import get_manager
            mgr = get_manager()
            
            # 1. Stop worker manager polling immediately to prevent new jobs
            log.dual_log(tag="App:Shutdown", message="Phase 1: Stopping worker manager polling loop", level="INFO")
            mgr.stop()
            
            # 2. Broadcast Cancellation to EXISTING workers
            # Use list() to create a snapshot and avoid RuntimeError if threads remove themselves during iteration
            log.dual_log(tag="App:Shutdown", message="Phase 2: Broadcasting cancellation to active workers", level="INFO")
            for flag in list(mgr.cancellation_flags.values()):
                flag.set()
            
            # 3. Drain (60s limit) - wait for active jobs to complete
            import asyncio
            import time
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
            
            # 4. Release browser resources
            log.dual_log(tag="App:Shutdown", message="Releasing browser resources", level="INFO")
            from utils.browser_daemon import daemon_manager
            daemon_manager.shutdown_driver()
            daemon_manager.surgical_kill()

            # 5. Wait for database writes and shutdown writer
            from database.writer import wait_for_writes, shutdown_writer
            await wait_for_writes()
            shutdown_writer()
            
            log.dual_log(tag="App:Shutdown", message="Clean shutdown complete", level="INFO")
        except Exception as e:
            log.dual_log(tag="App:Shutdown", message=f"Shutdown error: {e}", level="ERROR")


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
