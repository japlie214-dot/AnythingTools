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
        # === Shutdown ===
        try:
            from bot.engine.worker import get_manager
            mgr = get_manager()
            mgr.stop()
            from database.writer import wait_for_writes, shutdown_writer
            await wait_for_writes()
            shutdown_writer()
            log.dual_log(tag="App:Shutdown", message="Worker and Writer threads stopped gracefully", level="INFO")
        except Exception as e:
            log.dual_log(tag="App:Shutdown", message=f"Error during shutdown: {e}", level="ERROR")


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
