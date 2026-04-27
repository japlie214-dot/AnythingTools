# utils/startup/database.py

from database.connection import DatabaseManager, SQLITE_VEC_AVAILABLE
from database.writer import start_writer
from database.lifecycle import run_database_lifecycle
from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)

async def init_database_layer() -> None:
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
        log.dual_log(tag="Startup:DB", message="SQLite pragmas tuned", level="INFO")
    except Exception as e:
        log.dual_log(tag="Startup:DB", message=f"Failed to tune SQLite pragmas: {e}", level="WARNING")

    start_writer()
    log.dual_log(tag="Startup:DB", message="Database writer thread started", level="INFO")

async def run_db_migrations() -> None:
    await run_database_lifecycle()
    log.dual_log(tag="Startup:DB", message="Database lifecycle completed", level="INFO")

async def validate_vec0() -> None:
    if not SQLITE_VEC_AVAILABLE:
        log.dual_log(tag="Startup:Vec0", message="sqlite_vec/vec0 extension not available; running in compatibility mode.", level="WARNING")
        return

    try:
        conn = DatabaseManager.get_read_connection()
        version = conn.execute("SELECT vec_version();").fetchone()
        log.dual_log(tag="Startup:Vec0", message=f"sqlite_vec/vec0 extension verified: {version[0]}", level="INFO")
    except Exception as e:
        log.dual_log(tag="Startup:Vec0", message=f"Vec0 validation skipped: {e}", level="WARNING")
