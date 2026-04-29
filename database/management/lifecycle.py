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


async def run_database_lifecycle() -> None:
    """Main database lifecycle: Iterates through all registered database contexts."""
    # 1. Import schemas (orchestration layer is allowed to know about domains)
    from database.schemas import (
        ALL_TABLES, ALL_VEC_TABLES, ALL_FTS_TABLES, ALL_TRIGGERS,
        MASTER_TABLES, LOGS_TABLES
    )
    
    # 2. Context 1: Main Operational Database
    log.dual_log(tag="DB:Lifecycle", message="Preparing validation for Operational DB")
    main_tables = {**ALL_TABLES, **ALL_VEC_TABLES, **ALL_FTS_TABLES}
    
    # Handle orphaned backup for main DB
    await _validate_single_db(
        label="Operational DB",
        db_manager=DatabaseManager,
        db_path=DB_PATH,
        expected_tables=main_tables,
        expected_triggers=ALL_TRIGGERS,
        master_tables=MASTER_TABLES
    )
    
    # 3. Context 2: Logs Database (with clean separation)
    log.dual_log(tag="DB:Lifecycle", message="---")
    log.dual_log(tag="DB:Lifecycle", message="Preparing validation for Logs DB")
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
    log.dual_log(tag="DB:Lifecycle", message=f"Initiating validation sequence for {label}")
    
    # 1. Restore orphaned backups if present (agnostic)
    try:
        restore_orphaned_backup(db_path)
    except Exception as e:
        log.dual_log(tag="DB:Lifecycle", level="CRITICAL", 
                    message=f"[{label}] Backup restoration failed: {e}")
        raise
    
    # 2. Check database state (agnostic)
    exists, is_corrupted = check_database_file_state(db_path)
    
    # 3. Handle fresh initialization
    if not exists:
        log.dual_log(tag="DB:Lifecycle", level="INFO", 
                    message=f"[{label}] Database not found, running fresh init")
        await _initialize_database(db_manager, label, expected_tables, expected_triggers)
        return
    
    # 4. Handle corrupted database
    if is_corrupted or db_path.stat().st_size == 0:
        if ALLOW_DESTRUCTIVE_RESET:
            log.dual_log(tag="DB:Lifecycle", level="CRITICAL", 
                        message=f"[{label}] Corrupted DB detected, executing destructive reset")
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
        
        # Log all actions
        for action in report.actions:
            level = "WARNING" if action.action in ["recreated", "pruned"] else "INFO"
            msg = f"[{label}] {action.action.upper()}: {action.table_name}"
            if action.reason:
                msg += f" ({action.reason})"
            log.dual_log(tag="DB:Lifecycle", level=level, message=msg)
        
        # Handle master table restoration
        if report.master_tables_recreated:
            await _restore_master_tables(conn, label, expected_tables, report.master_tables_recreated)
        
        # Final checkpoint
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
        log.dual_log(tag="DB:Lifecycle", level="INFO", 
                    message=f"[{label}] Validation complete")
        
    except Exception as e:
        log.dual_log(tag="DB:Lifecycle", level="CRITICAL", 
                    message=f"[{label}] Validation failed: {e}", exc_info=e)
        conn.rollback()
        raise RuntimeError(f"[{label}] Validation failed: {e}") from e
    finally:
        conn.close()


async def _initialize_database(db_manager, label: str, expected_tables: dict, expected_triggers: dict):
    """Initialize a fresh database with provided schemas."""
    log.dual_log(tag="DB:Lifecycle", level="INFO", message=f"[{label}] Initializing fresh database")
    
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
        
        log.dual_log(tag="DB:Lifecycle", level="INFO", 
                    message=f"[{label}] Initialization successful")
        
    except Exception as e:
        log.dual_log(tag="DB:Lifecycle", level="ERROR", 
                    message=f"[{label}] Initialization failed: {e}", exc_info=e)
        raise RuntimeError(f"[{label}] Failed to initialize: {e}") from e


async def _restore_master_tables(conn: sqlite3.Connection, label: str, expected_tables: dict, master_tables: list):
    """Restore master tables from backup after recreation - agnostic."""
    log.dual_log(tag="DB:Lifecycle", level="WARNING", 
                message=f"[{label}] Master tables need restoration: {master_tables}")
    
    try:
        from database.backup.restore import restore_master_tables_direct
        result = restore_master_tables_direct(conn, master_tables)
        
        if result.success:
            log.dual_log(tag="DB:Lifecycle", level="INFO", 
                       message=f"[{label}] Restored master tables: {result.restored_counts}")
            
            # Agnostic FTS rebuild: Check if any expected FTS table targets the restored tables
            for table_name in master_tables:
                # Look for FTS tables in expected_tables that match this master table
                for fts_name in expected_tables.keys():
                    if fts_name.endswith("_fts") and fts_name.startswith(table_name.replace("_vec", "")):
                        try:
                            conn.execute(f"INSERT INTO {fts_name}({fts_name}) VALUES('rebuild')")
                            log.dual_log(tag="DB:Lifecycle", level="INFO", 
                                       message=f"[{label}] FTS5 index {fts_name} rebuilt")
                        except sqlite3.OperationalError:
                            pass
            
            # Post-restore cleanup backup
            log.dual_log(tag="DB:Lifecycle", level="INFO", 
                       message=f"[{label}] Running post-restore backup")
            from database.backup.storage import export_all_tables
            result = export_all_tables(conn, mode="full")
            
            if result.success:
                log.dual_log(tag="DB:Lifecycle", level="INFO", 
                           message=f"[{label}] Cleanup backup complete")
            else:
                log.dual_log(tag="DB:Lifecycle", level="WARNING", 
                           message=f"[{label}] Cleanup backup failed: {result.error}")
        else:
            log.dual_log(tag="DB:Lifecycle", level="CRITICAL", 
                       message=f"[{label}] Restoration failed: {result.error}")
    except Exception as e:
        log.dual_log(tag="DB:Lifecycle", level="CRITICAL", 
                    message=f"[{label}] Restoration error: {e}", exc_info=e)
        raise
