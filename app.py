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

from fastapi import FastAPI
import logging

import config as config_module

from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bind the JobCompletionRegistry to the running event loop.
    # MUST happen before any sync API route is hit. Ref:
    # https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.call_soon_threadsafe
    from bot.engine.completion_registry import job_completion_registry
    job_completion_registry.bind_loop(asyncio.get_running_loop())

    # Enforce single-process execution to protect manifest integrity
    if int(os.environ.get("WEB_CONCURRENCY", "1")) > 1:
        raise RuntimeError("CRITICAL: AnythingTools must be run with workers=1 to prevent manifest corruption.")
        
    startup_failed = False
    try:
        from utils.startup import run_startup
        await run_startup(app)
        log.dual_log(tag="App:Lifecycle:StartupSuccess", message="Startup completed successfully", level="INFO", payload={"status": "success"})

        yield
    except Exception as e:
        log.dual_log(tag="App:Lifecycle:StartupFailed", message=f"Startup aborted: {e}", level="CRITICAL", payload={"error": str(e), "startup_failed": True})
        startup_failed = True
    finally:
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

            # Staging Wipe on Shutdown
            from config import DATABASE_STAGING_ENABLED, DATABASE_STAGING_WIPE_ON_STARTUP
            if DATABASE_STAGING_ENABLED and DATABASE_STAGING_WIPE_ON_STARTUP:
                from database.backup.staging import StagingWipeService
                try:
                    from database.backup.engine.cloud_engine import _global_cloud_engine
                    if _global_cloud_engine and _global_cloud_engine.engine:
                        sf_result = await asyncio.to_thread(
                            StagingWipeService.wipe_snowflake, _global_cloud_engine
                        )
                        log.dual_log(
                            tag="App:Shutdown:StagingWipe:Snowflake",
                            message="Snowflake staging tables wiped on shutdown",
                            level="INFO",
                            payload=sf_result,
                        )
                except Exception as e:
                    log.dual_log(
                        tag="App:Shutdown:StagingWipe:Snowflake:Failed",
                        message=f"Snowflake staging wipe on shutdown failed: {e}",
                        level="WARNING",
                        payload={"error": str(e)},
                    )
                sqlite_result = await asyncio.to_thread(StagingWipeService.wipe_sqlite)
                log.dual_log(
                    tag="App:Shutdown:StagingWipe:SQLite",
                    message="SQLite staging tables wiped on shutdown",
                    level="INFO",
                    payload=sqlite_result,
                )

            # Shutdown Backup V2
            try:
                from utils.startup import _global_sync_engine
                from database.backup.writer.cloud_writer import cloud_write_queue
                
                # Drain cloud writer queue
                drain_start = time.time()
                while cloud_write_queue.unfinished_tasks > 0 and (time.time() - drain_start < 10.0):
                    await asyncio.sleep(0.5)

                if _global_sync_engine:
                    from config import DATABASE_STAGING_ENABLED
                    if not DATABASE_STAGING_ENABLED:
                        log.dual_log(tag="App:Lifecycle:ShutdownSync", message="Running shutdown sync", level="INFO", payload={"phase": "shutdown_sync"})
                        try:
                            sync_result = await asyncio.wait_for(
                                asyncio.to_thread(_global_sync_engine.sync_all, "delta"),
                                timeout=30.0
                            )
                            log.dual_log(tag="App:Lifecycle:ShutdownSyncComplete", message="Shutdown sync completed", level="INFO", payload=sync_result)
                        except asyncio.TimeoutError:
                            log.dual_log(tag="App:Lifecycle:ShutdownSyncTimeout", message="Shutdown sync timed out", level="WARNING", payload={"timeout_s": 30.0})
                    else:
                        log.dual_log(tag="App:Lifecycle:ShutdownSync", message="Staging mode — skipping shutdown sync", level="INFO", payload={"phase": "shutdown_sync"})
                    
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

app.add_middleware(
    CORSMiddleware,
    # allow_credentials=True is incompatible with allow_origins=["*"] per the W3C CORS spec.
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === Mount routers ===
from api.routes import router as api_router
# /manifest is defined on api_router itself (api/routes.py:41), so a single
# include covers all /api/* endpoints. The previous public_router_no_auth
# block was redundant once auth was removed.
app.include_router(api_router, prefix="/api")

@app.get("/", include_in_schema=False)
async def root():
    return {"message": "AnythingTools API", "version": "1.0.0", "docs": "/docs"}
