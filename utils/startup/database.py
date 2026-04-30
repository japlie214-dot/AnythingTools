# utils/startup/database.py
"""Database orchestration initialization with transparent file logging."""

from database.connection import DatabaseManager, LogsDatabaseManager, SQLITE_VEC_AVAILABLE, DB_PATH, LOGS_DB_PATH
from database.writer import start_writer
from database.management.lifecycle import run_database_lifecycle
from utils.logger.core import get_dual_logger
import sys

log = get_dual_logger(__name__)


async def init_database_layer() -> None:
    """Initialize database layer with explicit file probing."""
    # Fresh Start Policy: Wipe ephemeral logs.db
    try:
        if LOGS_DB_PATH.exists():
            # Close any zombie handles before unlinking
            import os
            from database.connection import LogsDatabaseManager
            LogsDatabaseManager.close_read_connection()
            LOGS_DB_PATH.unlink(missing_ok=True)
            # Remove sidecars
            for s in ["-wal", "-shm"]:
                (LOGS_DB_PATH.parent / (LOGS_DB_PATH.name + s)).unlink(missing_ok=True)
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to clear logs.db: {e}\n")

    # 1. Log file probing
    for label, path in [("Main", DB_PATH), ("Logs", LOGS_DB_PATH)]:
        log.dual_log(
            tag="Startup:DB",
            message=f"Probing {label} DB",
            payload={"label": label, "path": str(path), "exists": path.exists()},
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
        log.dual_log(tag="Startup:DB", message="Main DB pragmas tuned", level="INFO", payload={"pragmas_applied": len(pragmas)})
    except Exception as e:
        log.dual_log(tag="Startup:DB", message="Failed to tune pragmas", level="WARNING", payload={"error": str(e)})
    
    # 3. Start writer threads
    start_writer()
    from database.logs_writer import start_logs_writer, verify_logs_readiness
    start_logs_writer()
    log.dual_log(tag="Startup:DB", message="All DB writer threads active", level="INFO", payload={"writers": ["main", "logs"]})
    
    # Verify logger readiness; abort if fails
    if not verify_logs_readiness():
        import os, signal, sys
        sys.stderr.write("[FATAL] Logs database readiness check failed. Aborting startup.\n")
        os.kill(os.getpid(), signal.SIGTERM)
        return
    
    # 4. Initialize log schema (if needed)
    try:
        from database.logs_writer import logs_enqueue_write
        from database.schemas import get_logs_init_script
        logs_enqueue_write("__EXEC_SCRIPT__", (get_logs_init_script(),))
        log.dual_log(tag="Startup:DB", message="Logs DB schema initialized", level="INFO", payload={"initialized": True})
    except Exception as e:
        log.dual_log(tag="Startup:DB", message="Logs init deferred", level="DEBUG", payload={"error": str(e)})


async def run_db_migrations() -> None:
    """Run full lifecycle validation."""
    log.dual_log(tag="Startup:DB", message="Starting database lifecycle validation", level="INFO", payload={"action": "validate_all"})
    await run_database_lifecycle()
    log.dual_log(tag="Startup:DB", message="Database lifecycle completed", level="INFO", payload={"action": "validate_all_completed"})


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
            message="sqlite_vec/vec0 extension verified",
            level="INFO",
            payload={"version": version[0]},
        )
    except Exception as e:
        log.dual_log(tag="Startup:Vec0", message="Vec0 validation skipped", level="WARNING", payload={"error": str(e)})
