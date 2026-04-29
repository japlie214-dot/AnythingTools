# utils/startup/database.py
"""Database orchestration initialization with transparent file logging."""

from database.connection import DatabaseManager, LogsDatabaseManager, SQLITE_VEC_AVAILABLE, DB_PATH, LOGS_DB_PATH
from database.writer import start_writer
from database.management.lifecycle import run_database_lifecycle
from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)


async def init_database_layer() -> None:
    """Initialize database layer with explicit file probing."""
    # 1. Log file probing
    for label, path in [("Main", DB_PATH), ("Logs", LOGS_DB_PATH)]:
        status = "found" if path.exists() else "not found (will create)"
        exists_str = "EXISTS" if path.exists() else "MISSING"
        log.dual_log(
            tag="Startup:DB", 
            message=f"Probing {label} DB: {path} -> {exists_str}"
        )
    
    # 2. Tune main DB pragmas
    pragmas = [
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA cache_size=-64000",
        "PRAGMA temp_store=MEMORY",
        "PRAGMA foreign_keys=ON",
        "PRAGMA mmap_size=268435456",
    ]
    
    try:
        conn = DatabaseManager.get_read_connection()
        for pragma in pragmas:
            try:
                conn.execute(pragma)
            except Exception:
                pass
        log.dual_log(tag="Startup:DB", message="Main DB pragmas tuned", level="INFO")
    except Exception as e:
        log.dual_log(tag="Startup:DB", message=f"Failed to tune pragmas: {e}", level="WARNING")
    
    # 3. Start writer threads
    start_writer()
    from database.logs_writer import start_logs_writer
    start_logs_writer()
    log.dual_log(tag="Startup:DB", message="All DB writer threads active", level="INFO")
    
    # 4. Initialize log schema (if needed)
    try:
        from database.logs_writer import logs_enqueue_write
        from database.schemas import get_logs_init_script
        logs_enqueue_write("__EXEC_SCRIPT__", (get_logs_init_script(),))
        log.dual_log(tag="Startup:DB", message="Logs DB schema initialized", level="INFO")
    except Exception as e:
        log.dual_log(tag="Startup:DB", message=f"Logs init deferred: {e}", level="DEBUG")


async def run_db_migrations() -> None:
    """Run full lifecycle validation."""
    log.dual_log(tag="Startup:DB", message="Starting database lifecycle validation", level="INFO")
    await run_database_lifecycle()
    log.dual_log(tag="Startup:DB", message="Database lifecycle completed", level="INFO")


async def validate_vec0() -> None:
    """Validate sqlite_vec extension."""
    if not SQLITE_VEC_AVAILABLE:
        log.dual_log(
            tag="Startup:Vec0", 
            message="sqlite_vec/vec0 extension not available; running in compatibility mode",
            level="WARNING"
        )
        return
    
    try:
        conn = DatabaseManager.get_read_connection()
        version = conn.execute("SELECT vec_version();").fetchone()
        log.dual_log(
            tag="Startup:Vec0", 
            message=f"sqlite_vec/vec0 extension verified: {version[0]}",
            level="INFO"
        )
    except Exception as e:
        log.dual_log(tag="Startup:Vec0", message=f"Vec0 validation skipped: {e}", level="WARNING")
