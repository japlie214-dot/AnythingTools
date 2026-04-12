# app.py
"""
AnythingTools FastAPI application entrypoint with lifespan hooks:
- mounts artifacts/
- runs zombie-chrome cleanup
- starts DB writer thread
- applies SQL migrations found under database/migrations/
"""

from fastapi import FastAPI, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.security.api_key import APIKeyHeader
from fastapi import Security, HTTPException
from contextlib import asynccontextmanager
import logging
import os
import asyncio
from pathlib import Path

import config as config_module


async def prefetch_paddleocr():
    try:
        def _init_ocr():
            from paddleocr import PaddleOCR
            PaddleOCR(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False
            )
        await asyncio.to_thread(_init_ocr)
        from utils.logger.core import get_dual_logger
        get_dual_logger(__name__).dual_log(tag="Sys:Startup:OCR", message="PaddleOCR models pre-fetched successfully.")
    except Exception as e:
        from utils.logger.core import get_dual_logger
        get_dual_logger(__name__).dual_log(tag="Sys:Startup:OCR", message=f"Failed to pre-fetch PaddleOCR: {e}", level="WARNING")

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
from database.schema import get_init_script

MIGRATIONS_DIR = Path(__file__).parent / "database" / "migrations"

log = get_dual_logger(__name__)


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

    try:
        import sqlite3 as _sq
        import sqlite_vec  # type: ignore
        _conn = _sq.connect(":memory:")
        _conn.enable_load_extension(True)
        sqlite_vec.load(_conn)
        _conn.close()
        log.dual_log(tag="Sys:Vec0", message="vec0 extension loaded successfully.", level="INFO")
    except Exception as e:
        log.dual_log(
            tag="Sys:Vec0",
            message=f"sqlite_vec failed to load at runtime: {e}; continuing in compatibility mode.",
            level="WARNING",
            exc_info=e,
        )


async def reconcile_pending_embeddings() -> None:
    """Startup healing pass: generate missing embeddings for scraped_articles left
    in embedding_status='PENDING' by prior incomplete runs.

    This is a background task started on app startup so it doesn't block the
    main event loop during warmup.
    """
    import sqlite3 as _sq
    import struct as _struct

    try:
        # Lazy import the snowflake client (may raise if credentials missing).
        from clients.snowflake_client import snowflake_client  # type: ignore
    except Exception:
        snowflake_client = None

    try:
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = _sq.Row
        rows = conn.execute(
            "SELECT id, vec_rowid, title, conclusion "
            "FROM scraped_articles WHERE embedding_status = 'PENDING'"
        ).fetchall()
        if not rows:
            return

        log.dual_log(tag="DB:Reconcile", message=f"Found {len(rows)} articles pending embedding; healing.")

        for row in rows:
            try:
                _text = f"{row['title'] or ''}: {row['conclusion'] or ''}".strip(": ")
                if not _text:
                    # Title and conclusion are both empty — nothing useful to embed.
                    enqueue_write(
                        "UPDATE scraped_articles SET embedding_status = 'SKIPPED' WHERE id = ?",
                        (row['id'],),
                    )
                    continue

                if snowflake_client is None:
                    # Can't generate embeddings without a configured client; leave as PENDING.
                    log.dual_log(
                        tag="DB:Reconcile",
                        message=f"Snowflake client unavailable; skipping embedding for article {row['id']}",
                        level="WARNING",
                    )
                    continue

                try:
                    emb_list = await snowflake_client.async_embed(_text)
                except AttributeError:
                    # Fallback for older Snowflake clients: run blocking call in thread
                    emb_list = await asyncio.to_thread(snowflake_client.embed, _text)
                emb_bytes = _struct.pack(f"{len(emb_list)}f", *emb_list)

                # Enqueue the vector insertion and mark the article EMBEDDED.
                enqueue_write(
                    "INSERT INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)",
                    (row['vec_rowid'], emb_bytes),
                )
                enqueue_write(
                    "UPDATE scraped_articles SET embedding_status = 'EMBEDDED' WHERE id = ?",
                    (row['id'],),
                )

            except Exception as e:
                log.dual_log(
                    tag="DB:Reconcile",
                    message=f"Failed to heal embedding for article {row['id']}: {e}",
                    level="WARNING",
                )
                # Row stays PENDING; next startup will retry.

    except Exception as e:
        log.dual_log(
            tag="DB:Reconcile",
            message=f"Reconciliation scan error: {e}",
            level="ERROR",
            exc_info=e,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("AnythingTools startup: lifecycle beginning")

    # 0) Vec0 validation (best-effort; non-fatal)
    try:
        await validate_vec0_extension()
    except Exception as e:
        logging.exception("validate_vec0_extension raised during startup: %s", e)

    # 1) Zombie Chrome cleanup (best-effort)
    try:
        if psutil is not None and config_module.CHROME_USER_DATA_DIR:
            chrome_dir = Path(config_module.CHROME_USER_DATA_DIR)
            for p in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    cmdline = p.info.get('cmdline') or []
                    if any(str(chrome_dir) in str(c) for c in cmdline):
                        log.dual_log(tag="Browser:Cleanup", message=f"Killing zombie chrome pid={p.pid} cmdline={cmdline}", level="DEBUG")
                        p.kill()
                except Exception:
                    logging.exception("Error while scanning processes for zombie-chrome")
    except Exception:
        logging.exception("Zombie-chrome cleanup failed; continuing startup")

    # 1.5) Ensure chrome_download/ exists for browser tools
    try:
        os.makedirs("chrome_download", exist_ok=True)
    except Exception:
        pass

    # === Step 2: Start DB writer early and initialize schema via the single-writer ===
    try:
        start_writer()
        log.dual_log(tag="DB:WriterStart", message="Database writer started.")
    except Exception as e:
        logging.exception("Failed to start DB writer thread: %s", e)

    # Enqueue authoritative schema init via writer execscript and wait for completion
    try:
        script = get_init_script()
        enqueue_execscript(script)
        try:
            await wait_for_writes()
            log.dual_log(tag="DB:Schema", message="Schema initialization completed via writer.")
        except Exception as e:
            log.dual_log(tag="DB:Schema", message=f"wait_for_writes failed: {e}", level="WARNING", exc_info=e)
    except Exception as e:
        log.dual_log(tag="DB:Schema", message=f"Failed to enqueue/initialize schema: {e}", level="ERROR", exc_info=e)

    # 3) Truncate transient PDF cache (non-fatal) — use single-writer enqueue + wait
    try:
        enqueue_write("DELETE FROM pdf_parsed_pages")
        try:
            await wait_for_writes()
        except Exception:
            pass
    except Exception:
        pass

    # 4) Warm up the singleton Driver (non-fatal if Chrome unavailable)
    try:
        from utils.browser_lock import browser_lock
        from utils.browser_daemon import get_or_create_driver
        from utils.browser_utils import safe_google_get

        try:
            await browser_lock.acquire()
            try:
                _warmup_driver = get_or_create_driver()
                safe_google_get(_warmup_driver, "https://www.google.com")
                # Defensive single-tab enforcement during warmup (idempotent).
                try:
                    from utils.som_utils import enforce_single_tab
                    try:
                        enforce_single_tab(_warmup_driver)
                    except Exception as e:
                        log.dual_log(tag="Browser:Warmup", message=f"enforce_single_tab failed during warmup: {e}", level="WARNING", exc_info=e)
                except Exception:
                    pass
                log.dual_log(tag="Browser:Warmup", message="Browser warm-up successful.")
            finally:
                browser_lock.release()
        except Exception as _we:
            log.dual_log(
                tag="Browser:Warmup",
                message=f"Browser warm-up failed — driver will initialize lazily: {_we}",
                level="WARNING",
            )
    except Exception:
        # If browser modules are unavailable entirely, continue silently.
        pass

    # 5) Startup Recovery Scan: mark RUNNING jobs as INTERRUPTED (best-effort)
    try:
        import sqlite3

        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT job_id FROM jobs WHERE status = 'RUNNING'").fetchall():
            enqueue_write(
                "UPDATE jobs SET status = 'INTERRUPTED', updated_at = datetime('now') WHERE job_id = ?",
                (row['job_id'],),
            )
        log.dual_log(tag="DB:Recovery", message="Startup recovery scan complete.")
    except Exception as e:
        log.dual_log(tag="DB:Recovery", message="Recovery scan error.", level="ERROR", exc_info=e)

    # 6) Stale Job Cleanup: purge job_items for jobs inactive > 7 days
    try:
        import sqlite3

        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT job_id FROM jobs WHERE status IN ('RUNNING','PENDING','INTERRUPTED') "
            "AND updated_at < datetime('now', '-7 days')"
        ).fetchall():
            enqueue_write("UPDATE jobs SET status = 'FAILED' WHERE job_id = ?", (row['job_id'],))
            enqueue_write("DELETE FROM job_items WHERE job_id = ?", (row['job_id'],))
        
        from database.writer import purge_stale_sessions
        purge_stale_sessions(7)
        log.dual_log(tag="DB:Cleanup", message="Stale job and session cleanup complete.")
    except Exception as e:
        log.dual_log(tag="DB:Cleanup", message="Cleanup error.", level="ERROR", exc_info=e)

    # 7) Load tool registry
    try:
        REGISTRY.load_all()
        log.dual_log(tag="Sys:Registry", message="Tool registry loaded.")
    except Exception as e:
        log.dual_log(tag="Sys:Registry", message="Tool registry load failed.", level="WARNING", exc_info=e)

    # 7) Pre-fetch PaddleOCR models
    try:
        asyncio.create_task(prefetch_paddleocr())
    except Exception:
        pass

    # 8) Background: reconcile any pending embeddings
    try:
        asyncio.create_task(reconcile_pending_embeddings())
    except Exception:
        pass

    yield

    logging.info("AnythingTools shutdown: lifecycle complete")

    # Shutdown: Truncate transient PDF cache again, stop writer, and shutdown driver.
    try:
        enqueue_write("DELETE FROM pdf_parsed_pages")
        try:
            await wait_for_writes()
        except Exception:
            pass
    except Exception:
        pass

    try:
        shutdown_writer()
        log.dual_log(tag="DB:WriterStop", message="Database writer stopped.")
    except Exception:
        logging.exception("Error while shutting down DB writer")

    try:
        from utils.browser_daemon import shutdown_driver

        shutdown_driver()
        log.dual_log(tag="Browser:Shutdown", message="Browser driver shut down.")
    except Exception:
        pass


app = FastAPI(lifespan=lifespan)
ARTIFACTS_DIR = Path(__file__).parent / config_module.ARTIFACTS_ROOT
try:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
except Exception as e:
    try:
        log.dual_log(tag="Sys:Artifacts", message=f"Failed to create artifacts dir {ARTIFACTS_DIR}: {e}", level="WARNING", exc_info=e)
    except Exception:
        pass
app.mount("/artifacts", StaticFiles(directory=str(ARTIFACTS_DIR)), name="artifacts")

# Include API routes if available
try:
    from api.routes import router as api_router  # type: ignore
    app.include_router(api_router, prefix="/api", dependencies=[Depends(verify_api_key)])
except Exception as e:
    logging.debug("api.routes not available yet: %s", e)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    try:
        from database.writer import write_queue
        qsize = write_queue.qsize()
    except Exception:
        qsize = 0
    return {
        "write_queue_size": qsize,
        "browser_healthy": False,
    }
