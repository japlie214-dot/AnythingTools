# database/management/lifecycle.py
"""Multi-DB Lifecycle Coordinator.

Orchestrates validation across multiple database files using agnostic reconciler.
Operates exclusively on schemas passed as arguments.
"""

import os
import sqlite3
import asyncio
from typing import Optional

from database.connection import DB_PATH, DatabaseManager, LogsDatabaseManager, LOGS_DB_PATH
from database.schemas import get_init_script, get_logs_init_script
from database.management.health import restore_orphaned_backup, check_database_file_state
from database.management.reconciler import SchemaReconciler, ReconciliationReport
from database.management.migration_types import TypeMismatchPlan, ColumnMismatch
from database.writer import start_writer, enqueue_write, enqueue_execscript, wait_for_writes
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

ALLOW_DESTRUCTIVE_RESET = os.getenv("SUMANAL_ALLOW_SCHEMA_RESET", "0") == "1"


def _remove_db_files(path) -> None:
    """Remove database files including WAL and SHM sidecars."""
    for suffix in ["", ".bak", "-wal", "-shm"]:
        target = path.with_suffix(path.suffix + suffix) if suffix != "" else path
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass


def migrate_drop_pending_callback_status() -> None:
    """One-time migration: rebuild jobs table to remove PENDING_CALLBACK enum.

    SQLite CHECK constraints cannot be altered in-place. The canonical
    migration pattern is clone-recreate-repopulate, documented at:
    https://www.sqlite.org/lang_altertable.html#otheralter

    This function:
    1. Checks sqlite_master.sql for the jobs table.
    2. If the SQL contains 'PENDING_CALLBACK', performs the rebuild.
    3. Any existing rows with status='PENDING_CALLBACK' are mapped to 'FAILED'
       (they were waiting for a callback delivery that will never come).
    4. The clone table is dropped after successful repopulation.
    """
    conn = DatabaseManager.create_write_connection()
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        if not row or not row[0]:
            return  # Table doesn't exist yet; reconciler will create it

        current_sql = row[0]
        if "PENDING_CALLBACK" not in current_sql:
            log.dual_log(
                tag="Migration:PendingCallback:Skip",
                message="jobs table already lacks PENDING_CALLBACK; no migration needed",
                payload={"action": "skip"}
            )
            return

        log.dual_log(
            tag="Migration:PendingCallback:Start",
            level="WARNING",
            message="Rebuilding jobs table to drop PENDING_CALLBACK enum",
            payload={"action": "start", "reason": "callback system removed"}
        )

        import time
        timestamp = int(time.time())
        clone_name = f"_migrate_jobs_{timestamp}"

        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            # 1. Clone the existing table
            conn.execute(f"ALTER TABLE jobs RENAME TO {clone_name}")

            # 2. Create the new table with updated DDL (from database/schemas/jobs.py)
            from database.schemas.jobs import TABLES
            conn.executescript(TABLES["jobs"])

            # 3. Repopulate, mapping PENDING_CALLBACK -> FAILED
            conn.execute("""
                INSERT INTO jobs (job_id, session_id, tool_name, args_json, status, retry_count, resume_count, created_at, updated_at, result_json)
                SELECT job_id, session_id, tool_name, args_json,
                       CASE WHEN status = 'PENDING_CALLBACK' THEN 'FAILED' ELSE status END,
                       retry_count, resume_count, created_at, updated_at, result_json
                FROM {}
            """.format(clone_name))

            # 4. Drop the clone
            conn.execute(f"DROP TABLE {clone_name}")
            conn.commit()

            migrated = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='FAILED'").fetchone()[0]
            log.dual_log(
                tag="Migration:PendingCallback:Complete",
                level="INFO",
                message="jobs table rebuilt without PENDING_CALLBACK",
                payload={"action": "complete", "clone_dropped": clone_name, "rows_migrated_to_failed": migrated}
            )
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    except Exception as e:
        log.dual_log(
            tag="Migration:PendingCallback:Error",
            level="CRITICAL",
            message=f"Failed to migrate jobs table: {e}",
            exc_info=e,
            payload={"error": str(e)}
        )
        conn.rollback()
        raise
    finally:
        conn.close()


async def run_database_lifecycle() -> None:
    """Main database lifecycle: Iterates through all registered database contexts."""
    # Pre-reconciliation: run the PENDING_CALLBACK migration if needed.
    # This must happen BEFORE the reconciler runs, because the reconciler
    # does not detect CHECK constraint changes (only column type/NOT NULL/PK).
    try:
        migrate_drop_pending_callback_status()
    except Exception as e:
        log.dual_log(
            tag="Database:Lifecycle:MigrationError",
            level="CRITICAL",
            message=f"Pre-reconciliation migration failed: {e}",
            payload={"error": str(e)}
        )
        # Continue with reconciliation — the reconciler may still be able to
        # handle the table if the migration partially succeeded.

    # 1. Import schemas (orchestration layer is allowed to know about domains)
    from database.schemas import (
        ALL_TABLES, ALL_VEC_TABLES, ALL_FTS_TABLES, ALL_TRIGGERS,
        PERSISTED_TABLES, LOGS_TABLES
    )
    
    # 2. Context 1: Main Operational Database
    log.dual_log(tag="Database:Lifecycle:Prepare", message="Preparing validation for Operational DB", payload={"db": "Operational DB"})
    main_tables = {**ALL_TABLES, **ALL_VEC_TABLES, **ALL_FTS_TABLES}
    
    # Handle orphaned backup for main DB
    await _validate_single_db(
        label="Operational DB",
        db_manager=DatabaseManager,
        db_path=DB_PATH,
        expected_tables=main_tables,
        expected_triggers=ALL_TRIGGERS,
        master_tables=PERSISTED_TABLES
    )
    
    # 3. Context 2: Logs Database (with clean separation)
    log.dual_log(tag="Database:Lifecycle:Separator", message="---", payload={"separator": True, "next_context": "Logs DB"})
    log.dual_log(tag="Database:Lifecycle:Prepare", message="Preparing validation for Logs DB", payload={"db": "Logs DB"})
    await _validate_single_db(
        label="Logs DB",
        db_manager=LogsDatabaseManager,
        db_path=LOGS_DB_PATH,
        expected_tables=LOGS_TABLES,
        expected_triggers={},
        master_tables=[]
    )


async def _validate_single_db(
    label: str,
    db_manager,
    db_path,
    expected_tables: dict,
    expected_triggers: dict,
    master_tables: list
) -> None:
    """Validate a single database file - completely agnostic logic."""
    log.dual_log(tag="Database:Lifecycle:Initiate", message="Initiating validation sequence", payload={"label": label})
    
    # 1. Restore orphaned backups if present (agnostic)
    try:
        restore_orphaned_backup(db_path)
    except Exception as e:
        log.dual_log(tag="Database:Lifecycle:RestoreFailed", level="CRITICAL",
                    message=f"[{label}] Backup restoration failed: {e}",
                    payload={"label": label, "error": str(e), "error_type": type(e).__name__})
        raise
    
    # 2. Check database state (agnostic)
    exists, is_corrupted = check_database_file_state(db_path)
    
    # 3. Handle fresh initialization
    if not exists:
        log.dual_log(tag="Database:Lifecycle:Missing", level="INFO",
                    message="Database not found, running fresh init", payload={"label": label})
        await _initialize_database(db_manager, label, expected_tables, expected_triggers)
        return
    
    # 4. Handle corrupted database
    if is_corrupted or db_path.stat().st_size == 0:
        if ALLOW_DESTRUCTIVE_RESET:
            log.dual_log(tag="Database:Lifecycle:Corrupted", level="CRITICAL",
                        message="Corrupted DB detected, executing destructive reset", payload={"label": label})
            _remove_db_files(db_path)
            await _initialize_database(db_manager, label, expected_tables, expected_triggers)
            return
        else:
            raise RuntimeError(f"[{label}] Database corrupted. Set SUMANAL_ALLOW_SCHEMA_RESET=1")
    
    # 5. Run reconciliation (pure agnostic)
    conn = db_manager.create_write_connection()
    try:
        # No specialized label checks here.
        # Reconciler will naturally prune any unexpected table (like 'logs' in main DB)
        # if it is not present in the 'expected_tables' dictionary.
        reconciler = SchemaReconciler(
            conn=conn,
            label=label,
            expected_tables=expected_tables,
            expected_triggers=expected_triggers,
            master_tables=master_tables
        )
        report = reconciler.reconcile()
        
        if hasattr(report, "type_mismatch_plans") and report.type_mismatch_plans:
            from database.management.migration_coordinator import DualDBMigrationCoordinator
            coordinator = DualDBMigrationCoordinator(conn, label)
            migration_records = coordinator.execute(report.type_mismatch_plans)
            
            for rec in migration_records:
                level = "WARNING" if rec.status == "failed" else "INFO"
                msg = f"[{label}] Migration {rec.phase} for {rec.table_name}: {rec.status}"
                log.dual_log(tag=f"Migration:{rec.phase}", level=level, message=msg, payload={"table": rec.table_name, "status": rec.status, "rows": rec.rows_affected})
            
            # Re-run trigger validation because table recreation drops attached triggers
            reconciler._validate_triggers(report)
        
        # Bump write generations so thread-local read connections see the new schema
        if label == "Logs DB":
            try:
                from database.logs_writer import _logs_writer_lock
                import database.logs_writer as lw
                with _logs_writer_lock:
                    lw._logs_write_generation += 1
            except Exception:
                pass
        else:
            try:
                from database.writer import _write_lock
                import database.writer as dw
                with _write_lock:
                    dw._write_generation += 1
            except Exception:
                pass

        # Log all actions
        for action in report.actions:
            level = "WARNING" if action.action in ["recreated", "pruned"] else "INFO"
            msg = f"[{label}] {action.action.upper()}: {action.table_name}"
            if action.reason:
                msg += f" ({action.reason})"
            log.dual_log(tag="Database:Lifecycle:Action", level=level, message=msg, payload={"label": label, "action": action.action, "table": action.table_name, "reason": action.reason})
        
        # Final checkpoint
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
        log.dual_log(tag="Database:Lifecycle:Validated", level="INFO",
                    message="Validation complete", payload={"label": label})
        
    except Exception as e:
        log.dual_log(tag="Database:Lifecycle:ValidationError", level="CRITICAL",
                    message="Validation failed", exc_info=e, payload={"label": label, "error": str(e)})
        conn.rollback()
        raise RuntimeError(f"[{label}] Validation failed: {e}") from e
    finally:
        conn.close()


async def _initialize_database(db_manager, label: str, expected_tables: dict, expected_triggers: dict):
    """Initialize a fresh database with provided schemas."""
    log.dual_log(tag="Database:Lifecycle:Initializing", level="INFO", message="Initializing fresh database", payload={"label": label})
    
    try:
        # Start writer
        start_writer()
        
        # Build init script from expected tables
        parts = []
        for _, ddl in expected_tables.items():
            parts.append(ddl)
        for _, ddl in expected_triggers.items():
            parts.append(ddl)
        init_script = "\n".join(parts)
        
        # Queue initialization
        if "Logs" in label:
            from database.logs_writer import logs_enqueue_write
            logs_enqueue_write("__EXEC_SCRIPT__", (init_script,))
        else:
            enqueue_execscript(init_script)
        
        await wait_for_writes(timeout=10.0)
        
        log.dual_log(tag="Database:Lifecycle:Initialized", level="INFO",
                    message="Initialization successful", payload={"label": label})
        
    except Exception as e:
        log.dual_log(tag="Database:Lifecycle:InitFailed", level="ERROR",
                    message="Initialization failed", exc_info=e, payload={"label": label, "error": str(e)})
        raise RuntimeError(f"[{label}] Failed to initialize: {e}") from e


