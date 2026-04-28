# database/lifecycle.py

import os
import sqlite3
import asyncio
from typing import Optional

from database.connection import DB_PATH, DatabaseManager
from database.schemas import get_init_script, get_repair_script
from database.health import check_database_file_state, restore_orphaned_backup
from database.reconciler import SchemaReconciler
from database.writer import (
    start_writer,
    enqueue_write,
    enqueue_execscript,
    wait_for_writes,
)
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

ALLOW_DESTRUCTIVE_RESET = os.getenv("SUMANAL_ALLOW_SCHEMA_RESET", "0") == "1"

def _remove_db_files() -> None:
    """Remove all database files including WAL and SHM sidecars."""
    for path in (
        DB_PATH,
        DB_PATH.with_suffix(".db.bak"),
        DB_PATH.with_name(f"{DB_PATH.name}-wal"),
        DB_PATH.with_name(f"{DB_PATH.name}-shm"),
    ):
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

async def initialize() -> None:
    """Initialize fresh database with canonical schema."""
    log.dual_log(tag="DB:Lifecycle", level="INFO", message="Initializing fresh database")
    
    try:
        start_writer() # Ensure writer runs for fresh init DDL writes
        enqueue_execscript(get_init_script())
        await wait_for_writes(timeout=10.0)
        
        from database.health import check_tables_exist
        all_present, missing = check_tables_exist()
        if not all_present:
            raise RuntimeError(f"Initialization incomplete, missing tables: {missing}")
    except Exception as e:
        log.dual_log(tag="DB:Lifecycle", level="ERROR", message=f"Init failed: {e}", exc_info=e)
        raise RuntimeError(f"Failed to initialize database: {e}") from e

async def run_database_lifecycle() -> None:
    """Main database lifecycle: reconciliation with intelligent restoration and cleanup."""
    # 1. Recover orphaned backups before checking state
    restore_orphaned_backup()
    
    # 2. Probe state
    exists, _ = check_database_file_state()
    
    if not exists:
        log.dual_log(tag="DB:Lifecycle", level="INFO", message="No database found, running fresh init.")
        await initialize()
        # After fresh init, reconciliation ensures all triggers are created
        await _reconcile_with_restoration()
        return

    # 3. Handle corrupted database
    if not exists or not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        if ALLOW_DESTRUCTIVE_RESET:
            log.dual_log(tag="DB:Lifecycle", level="CRITICAL", message="Corrupted DB, destructive reset executing.")
            _remove_db_files()
            await initialize()
            await _reconcile_with_restoration()
            return
        else:
            raise RuntimeError("Database file corrupted. Set SUMANAL_ALLOW_SCHEMA_RESET=1 to reset.")

    # 4. Run reconciliation for existing database
    await _reconcile_with_restoration()

async def _reconcile_with_restoration() -> None:
    """Reconcile schema and restore master tables if needed, with post-restore cleanup."""
    log.dual_log(tag="DB:Lifecycle", level="INFO", message="Starting schema reconciliation")
    
    conn = DatabaseManager.create_write_connection()
    try:
        # Force fresh logs table on startup
        conn.execute("DROP TABLE IF EXISTS logs")
        conn.commit()
        log.dual_log(tag="DB:Lifecycle", level="INFO", message="Dropped logs table for fresh startup.")

        # Run reconciliation
        reconciler = SchemaReconciler(conn)
        report = reconciler.reconcile()
        
        # Log all actions
        for action in report.actions:
            if action.action == "created":
                log.dual_log(tag="DB:Lifecycle", level="INFO", 
                           message=f"Created: {action.table_name} {'(master)' if action.is_master else ''}")
            elif action.action == "altered":
                log.dual_log(tag="DB:Lifecycle", level="INFO", 
                           message=f"Altered: {action.table_name} - {action.reason}")
            elif action.action == "recreated":
                level = "WARNING" if action.is_master else "INFO"
                log.dual_log(tag="DB:Lifecycle", level=level, 
                           message=f"Recreated: {action.table_name} - {action.reason} {'(master)' if action.is_master else ''}")

        # 5. Restore master tables if they were recreated
        if report.master_tables_recreated:
            log.dual_log(tag="DB:Lifecycle", level="WARNING", 
                       message=f"Master tables need restoration: {report.master_tables_recreated}")
            
            from database.backup.restore import restore_master_tables_direct
            restore_result = restore_master_tables_direct(conn, report.master_tables_recreated)
            
            if restore_result.success:
                log.dual_log(tag="DB:Lifecycle", level="INFO", 
                           message=f"Restored master tables: {restore_result.restored_counts}")
                
                # 6. Rebuild FTS5 if scraped_articles was restored
                if "scraped_articles" in report.master_tables_recreated:
                    try:
                        conn.execute("INSERT INTO scraped_articles_fts(scraped_articles_fts) VALUES('rebuild')")
                        log.dual_log(tag="DB:Lifecycle", level="INFO", message="FTS5 index rebuilt")
                    except sqlite3.OperationalError as e:
                        log.dual_log(tag="DB:Lifecycle", level="WARNING", message=f"FTS5 rebuild warning: {e}")
                
                # 7. Post-restore cleanup: Run full backup to replace Pre-Drop Snapshots
                log.dual_log(tag="DB:Lifecycle", level="INFO", message="Running full backup for cleanup")
                from database.backup.storage import export_all_tables
                export_result = export_all_tables(conn, mode="full")
                
                if export_result.success:
                    log.dual_log(tag="DB:Lifecycle", level="INFO", 
                               message=f"Cleanup backup complete: {export_result.exported_counts}")
                else:
                    log.dual_log(tag="DB:Lifecycle", level="WARNING", 
                               message=f"Cleanup backup failed: {export_result.error}")
            else:
                log.dual_log(tag="DB:Lifecycle", level="CRITICAL", 
                           message=f"Restoration failed: {restore_result.error}")

        # 8. Final checkpoint and commit
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
        log.dual_log(tag="DB:Lifecycle", level="INFO", message="Schema reconciliation completed")
        
    except Exception as e:
        log.dual_log(tag="DB:Lifecycle", level="CRITICAL", message=f"Reconciliation failed: {e}", exc_info=e)
        conn.rollback()
        raise RuntimeError(f"Database reconciliation failed: {e}") from e
    finally:
        conn.close()
