# database/schema.py

import os
import sqlite3

from database.connection import DB_PATH, DatabaseManager
from database.schemas import get_init_script as _get_init_script
from database.schemas import get_repair_script as _get_repair_script
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

# Constants only at module level; avoid import-time side-effects
ALLOW_DESTRUCTIVE_RESET = os.getenv("SUMANAL_ALLOW_SCHEMA_RESET", "0") == "1"

def get_schema_version() -> int:
    """Dynamic version check to avoid side-effects during import."""
    from database.migrations import get_latest_version
    return get_latest_version()

def get_init_script() -> str:
    return _get_init_script()

def get_repair_script(table_name: str) -> str | None:
    return _get_repair_script(table_name)

def _remove_db_files() -> None:
    """Delete the primary SQLite file and its -wal and -shm side‑car files."""
    for path in (
        DB_PATH,
        DB_PATH.with_name(f"{DB_PATH.name}-wal"),
        DB_PATH.with_name(f"{DB_PATH.name}-shm"),
    ):
        if path.exists():
            path.unlink()

def init_db() -> None:
    """Initialize (or migrate) the database schema."""
    conn = DatabaseManager.create_write_connection()
    try:
        cur = conn.cursor()
        try:
            current_v = cur.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError:
            current_v = 0
        
        schema_version = get_schema_version()
        
        if current_v != schema_version and ALLOW_DESTRUCTIVE_RESET:
            log.dual_log(tag="DB:Schema", level="WARNING", message=f"Destructive reset to v{schema_version}.")
            from database.schemas import ALL_TABLES, ALL_VEC_TABLES
            legacy_tables = list(ALL_TABLES.keys()) + list(ALL_VEC_TABLES.keys()) + [
                'sessions', 'execution_ledger', 'active_chat_state', 'tool_telemetry', 'grouped_formulas',
                'job_cache', 'chat_history', 'chat_messages', 'browser_macros', 'ai_skills'
            ]
            for t in legacy_tables:
                conn.execute(f"DROP TABLE IF EXISTS {t}")
            conn.commit()
            current_v = 0
            
        script = get_init_script()
        conn.executescript(script)
        
        from database.migrations import run_migrations
        run_migrations(conn)
    finally:
        conn.close()
