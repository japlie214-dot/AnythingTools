# database/lifecycle.py

import os
import sqlite3
import asyncio
from typing import Optional

from database.connection import DB_PATH, DatabaseManager
from database.schemas import BASE_SCHEMA_VERSION, get_init_script, get_repair_script
from database.health import (
    check_database_file_state,
    check_tables_exist,
    restore_orphaned_backup
)
from database.writer import (
    start_writer,
    enqueue_write,
    enqueue_execscript,
    wait_for_writes,
)
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

ALLOW_DESTRUCTIVE_RESET = os.getenv("SUMANAL_ALLOW_SCHEMA_RESET", "0") == "1"

def _get_target_version() -> int:
    return BASE_SCHEMA_VERSION

def _remove_db_files() -> None:
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

async def _repair_missing_tables(missing: list[str]) -> None:
    start_writer()
    for table_name in missing:
        script = get_repair_script(table_name)
        if script:
            enqueue_execscript(script)
            log.dual_log(tag="DB:Lifecycle", level="INFO", message=f"Queued repair for table: {table_name}")
        else:
            log.dual_log(tag="DB:Lifecycle", level="WARNING", message=f"No repair script for table: {table_name}")
    await wait_for_writes(timeout=10.0)

async def initialize() -> None:
    target_version = _get_target_version()
    log.dual_log(tag="DB:Lifecycle", level="INFO", message=f"Initializing fresh database to v{target_version}")
    
    try:
        start_writer() # Ensure writer runs for fresh init DDL writes
        enqueue_execscript(get_init_script())
        enqueue_write(f"PRAGMA user_version = {target_version}")
        await wait_for_writes(timeout=10.0)
        
        all_present, missing = check_tables_exist()
        if not all_present:
            raise RuntimeError(f"Initialization incomplete, missing tables: {missing}")
    except Exception as e:
        log.dual_log(tag="DB:Lifecycle", level="ERROR", message=f"Init failed: {e}", exc_info=e)
        raise RuntimeError(f"Failed to initialize database: {e}") from e

async def migrate(current_version: int) -> None:
    from database.migrations import run_migrations
    log.dual_log(tag="DB:Lifecycle", level="INFO", message=f"Starting migration from v{current_version}")
    
    # Direct isolated connection to ensure EXCLUSIVE transaction avoids writer contention
    conn = DatabaseManager.create_write_connection()
    try:
        conn.executescript(get_init_script())
        conn.commit()
        run_migrations(conn)
        
        all_present, missing = check_tables_exist(conn)
        if not all_present:
            log.dual_log(tag="DB:Lifecycle", level="WARNING", message=f"Missing tables after migration: {missing}")
            # Ensure tables are fully repaired before allowing startup to proceed
            await _repair_missing_tables(missing)
    finally:
        conn.close()

async def run_database_lifecycle() -> None:
    target_version = _get_target_version()
    
    # 1. Recover orphaned backups before checking state
    restore_orphaned_backup()
    
    # 2. Probe state
    exists, current_version = check_database_file_state()
    
    if not exists:
        log.dual_log(tag="DB:Lifecycle", level="INFO", message="No database found, running fresh init.")
        await initialize()
        return

    if current_version is None:
        if ALLOW_DESTRUCTIVE_RESET:
            log.dual_log(tag="DB:Lifecycle", level="CRITICAL", message="Corrupted DB, destructive reset executing.")
            _remove_db_files()
            await initialize()
            return
        else:
            raise RuntimeError("Database file corrupted. Set SUMANAL_ALLOW_SCHEMA_RESET=1 to reset.")
            
    if current_version == 0:
        log.dual_log(tag="DB:Lifecycle", level="WARNING", message="Existing database at v0, initializing.")
        await initialize()
    elif current_version < target_version:
        await migrate(current_version)
    elif current_version == target_version:
        all_present, missing = check_tables_exist()
        if not all_present:
            await _repair_missing_tables(missing)
    elif current_version > target_version:
        raise RuntimeError(f"Database version v{current_version} exceeds code target v{target_version}.")
