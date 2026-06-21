# app.py
"""
AnythingTools FastAPI application entrypoint with lifespan hooks:
- mounts artifacts/
- runs zombie-chrome cleanup
- starts DB writer thread
- applies SQL migrations found under database/migrations/
python -m uvicorn app:app --reload --port 8000
"""

import os
import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.security.api_key import APIKeyHeader
from fastapi import Security, HTTPException
import logging

import config as config_module

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=True)


from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize SSE registries on the running event loop. MUST happen before
    # any SSE route is hit. Ref:
    # https://docs.python.org/3/library/asyncio-sync.html#asyncio.Event
    from api.sse import shutdown, log_notify
    shutdown.init_shutdown_registry(asyncio.get_running_loop())
    log_notify.init_log_notify_bus(asyncio.get_running_loop())

    # Enforce single-process execution to protect manifest integrity
    if int(os.environ.get("WEB_CONCURRENCY", "1")) > 1:
        raise RuntimeError("CRITICAL: AnythingTools must be run with workers=1 to prevent manifest corruption.")
        
    startup_failed = False
    try:
        from utils.startup import run_startup
        await run_startup(app)
        log.dual_log(tag="App:Lifecycle:StartupSuccess", message="Startup completed successfully", level="INFO", payload={"status": "success"})

        # Retire legacy PENDING_CALLBACK rows AFTER DB init but BEFORE the
        # worker manager starts picking up jobs. Per Pushback 3: standalone
        # data mutation, NOT routed through DualDBMigrationCoordinator.
        try:
            from database.sse_retire_pending_callback import retire_pending_callback_jobs
            retired = retire_pending_callback_jobs()
            if retired:
                log.dual_log(tag="App:Lifecycle:RetiredPcb", message=f"Retired {retired} PENDING_CALLBACK job(s)", level="INFO", payload={"retired_count": retired})
        except Exception as e:
            log.dual_log(tag="App:Lifecycle:RetirePcbError", message=f"PENDING_CALLBACK retirement failed: {e}", level="WARNING", payload={"error": str(e)})
        yield
    except Exception as e:
        log.dual_log(tag="App:Lifecycle:StartupFailed", message=f"Startup aborted: {e}", level="CRITICAL", payload={"error": str(e), "startup_failed": True})
        startup_failed = True
    finally:
        # Signal SSE projectors to emit `server shutting down` BEFORE the
        # 60s _active_jobs drain. Per Pushback 6: os._exit(1) at line 125
        # would otherwise kill generators without a final event.
        try:
            from api.sse import shutdown as sse_shutdown
            sse_shutdown.signal_shutdown()
            # Give projectors ~3s to emit final events and close connections.
            await asyncio.sleep(3.0)
        except Exception:
            pass

        log.dual_log(tag="App:Lifecycle:ShutdownStarted", message="Initiating shutdown sequence...", level="INFO", payload={"phase": "init", "startup_failed": startup_failed})
        
        try:
            from bot.engine.worker import get_manager
            mgr = get_manager()
            
            if mgr:
                log.dual_log(tag="App:Lifecycle:ShutdownPhase1", message="Phase 1: Stopping worker manager polling loop", level="INFO", payload={"action": "stop_poll", "active_jobs": len(mgr._active_jobs)})
                mgr.stop()
                
                log.dual_log(tag="App:Lifecycle:ShutdownPhase2", message="Phase 2: Broadcasting cancellation to active workers", level="INFO", payload={"action": "broadcast_cancel", "cancellation_flags": len(mgr.cancellation_flags)})
                for flag in list(mgr.cancellation_flags.values()):
                    flag.set()
                
                drain_start = time.time()
                drain_timeout = 60.0
                
                log.dual_log(tag="App:Lifecycle:ShutdownPhase3", message="Draining active jobs", level="INFO", payload={"phase": 3, "drain_timeout_s": drain_timeout, "active_jobs": len(mgr._active_jobs)})
                while mgr._active_jobs and (time.time() - drain_start < drain_timeout):
                    remaining = len(mgr._active_jobs)
                    log.dual_log(tag="App:Lifecycle:ShutdownDraining", message="Draining active jobs", payload={"remaining": remaining, "elapsed_s": round(time.time() - drain_start, 1)})
                    await asyncio.sleep(2)
                
                if mgr._active_jobs:
                    log.dual_log(tag="App:Lifecycle:ShutdownTimeout", message=f"Drain timeout exceeded, {len(mgr._active_jobs)} job(s) remaining", level="WARNING", payload={"remaining": len(mgr._active_jobs)})
                else:
                    log.dual_log(tag="App:Lifecycle:ShutdownDrained", message="All active jobs drained successfully", level="INFO", payload={"drained": True, "elapsed_s": round(time.time() - drain_start, 1)})
            
            from utils.browser_daemon import daemon_manager
            log.dual_log(tag="App:Lifecycle:ShutdownBrowser", message="Releasing browser resources", level="INFO", payload={"daemon_pid": getattr(daemon_manager, "pid", None)})
            daemon_manager.shutdown_driver()
            daemon_manager.surgical_kill()

            from database.writer import wait_for_writes, shutdown_writer
            await wait_for_writes()
            shutdown_writer()

            # Shutdown Backup V2
            try:
                from utils.startup import _global_sync_engine
                from database.backup.writer.cloud_writer import cloud_write_queue
                
                # Drain cloud writer queue
                drain_start = time.time()
                while cloud_write_queue.unfinished_tasks > 0 and (time.time() - drain_start < 10.0):
                    await asyncio.sleep(0.5)

                if _global_sync_engine:
                    log.dual_log(tag="App:Lifecycle:ShutdownSync", message="Running shutdown sync", level="INFO", payload={"phase": "shutdown_sync"})
                    try:
                        sync_result = await asyncio.wait_for(
                            asyncio.to_thread(_global_sync_engine.sync_all, "delta"),
                            timeout=30.0
                        )
                        log.dual_log(tag="App:Lifecycle:ShutdownSyncComplete", message="Shutdown sync completed", level="INFO", payload=sync_result)
                    except asyncio.TimeoutError:
                        log.dual_log(tag="App:Lifecycle:ShutdownSyncTimeout", message="Shutdown sync timed out", level="WARNING", payload={"timeout_s": 30.0})
                    
                    _global_sync_engine.shutdown()
            except Exception as e:
                log.dual_log(tag="App:Lifecycle:ShutdownSyncError", message=f"Error closing SyncEngine: {e}", level="WARNING", payload={"error": str(e)})
            
            log.dual_log(tag="App:Lifecycle:ShutdownComplete", message="Clean shutdown complete", level="INFO", payload={"status": "clean", "startup_failed": startup_failed})
        except Exception as e:
            log.dual_log(tag="App:Lifecycle:ShutdownError", message=f"Shutdown error: {e}", level="ERROR", payload={"error": str(e)})
        finally:
            os._exit(1 if startup_failed else 0)


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
# /manifest is defined on api_router itself (api/routes.py:41), so a single
# include covers all /api/* endpoints. The previous public_router_no_auth
# block was redundant once auth was removed.
app.include_router(api_router, prefix="/api")

@app.get("/", include_in_schema=False)
async def root():
    return {"message": "AnythingTools API", "version": "1.0.0", "docs": "/docs"}
