# utils/startup/database.py
"""Database orchestration initialization with transparent file logging."""

import os
import sys
import signal
from database.connection import DatabaseManager, LogsDatabaseManager, SQLITE_VEC_AVAILABLE, DB_PATH, LOGS_DB_PATH
from database.schemas import get_logs_init_script
from database.writer import start_writer
from database.management.lifecycle import run_database_lifecycle
from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)


async def init_database_layer() -> None:
    """Initialize database layer with explicit file probing and fresh logs.db policy."""
    # Fresh Start Policy: Wipe ephemeral logs.db
    try:
        if LOGS_DB_PATH.exists():
            # Close any zombie handles before unlinking
            LogsDatabaseManager.close_read_connection()
            try:
                LOGS_DB_PATH.unlink(missing_ok=True)
                for s in ["-wal", "-shm"]:
                    (LOGS_DB_PATH.parent / (LOGS_DB_PATH.name + s)).unlink(missing_ok=True)
                sys.stderr.write("Logs database file unlinked successfully.\n")
            except Exception as file_e:
                sys.stderr.write(f"Warning: Could not unlink logs.db ({file_e}). Will drop table via SQL.\n")
    except Exception as e:
        sys.stderr.write(f"Warning: Failed during logs.db wipe policy: {e}\n")

    # Synchronously ensure the logs table is fresh and exists
    try:
        logs_conn = LogsDatabaseManager.create_write_connection()
        logs_conn.execute("DROP TABLE IF EXISTS logs")
        logs_conn.executescript(get_logs_init_script())
        logs_conn.commit()
        logs_conn.close()
        sys.stderr.write("Logs DB schema initialized to start fresh.\n")
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to synchronously initialize logs DB: {e}\n")

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
    
    # 4. Verify logger readiness; abort if fails
    if not verify_logs_readiness():
        sys.stderr.write("[FATAL] Logs database readiness check failed. Aborting startup.\n")
        os.kill(os.getpid(), signal.SIGTERM)
        return

    log.dual_log(
        tag="Startup:DB",
        message="Logs DB schema initialized and verified",
        level="INFO",
        payload={"initialized": True, "action": "recreated_logs_table"}
    )


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
            level="WARNING",
            payload={"extension": "sqlite_vec", "available": False, "mode": "compatibility"}
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
