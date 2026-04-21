# database/health.py

import sqlite3
import shutil
from typing import List, Tuple, Optional

from database.connection import DB_PATH, DatabaseManager
from database.schemas import ALL_TABLES, ALL_VEC_TABLES
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

EXPECTED_TABLES: List[str] = sorted(list(ALL_TABLES.keys()) + list(ALL_VEC_TABLES.keys()))

def list_expected_tables() -> List[str]:
    return EXPECTED_TABLES.copy()

def restore_orphaned_backup() -> None:
    """Detect and restore orphaned backup files from interrupted migrations. Hard fail if corrupted."""
    backup_path = DB_PATH.with_suffix(".db.bak")
    if not backup_path.exists():
        return
    
    if backup_path.stat().st_size == 0:
        log.dual_log(tag="DB:Health", level="CRITICAL", message="Orphaned backup is 0 bytes. Halting.")
        raise RuntimeError("Orphaned backup is 0 bytes. Halting restoration.")
    
    try:
        log.dual_log(tag="DB:Health", level="WARNING", message="Orphaned backup detected. Restoring.")
        for suffix in ["-wal", "-shm"]:
            sidecar = DB_PATH.with_name(DB_PATH.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
        
        shutil.copy2(backup_path, DB_PATH)
        backup_path.unlink()
        log.dual_log(tag="DB:Health", level="INFO", message="Orphaned backup restored successfully.")
    except Exception as e:
        log.dual_log(tag="DB:Health", level="CRITICAL", message=f"Failed to restore orphaned backup: {e}")
        raise RuntimeError(f"Failed to restore orphaned backup: {e}") from e

def check_database_file_state() -> Tuple[bool, Optional[int]]:
    """Check database existence and user_version using a raw, isolated connection to avoid poisoning."""
    if not DB_PATH.exists():
        return False, None
    if DB_PATH.stat().st_size == 0:
        # File exists but is 0 bytes, which is a form of corruption/incomplete state
        return True, None
    
    try:
        # Use an isolated connection to prevent poisoning DatabaseManager thread-local cache
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute("PRAGMA user_version").fetchone()
        version = row[0] if row else 0
        conn.close()
        return True, version
    except sqlite3.DatabaseError:
        return True, None
    except Exception:
        return True, None

def check_tables_exist(conn: Optional[sqlite3.Connection] = None) -> Tuple[bool, List[str]]:
    should_close = conn is None
    try:
        if conn is None:
            conn = DatabaseManager.get_read_connection()
        
        existing = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = [t for t in EXPECTED_TABLES if t not in existing]
        
        if missing:
            log.dual_log(tag="DB:Health", level="WARNING", message=f"Missing tables: {missing}")
        else:
            log.dual_log(tag="DB:Health", level="INFO", message="All expected tables verified.")
            
        return len(missing) == 0, missing
    except Exception as e:
        log.dual_log(tag="DB:Health", level="ERROR", message=f"Health check failed: {e}", exc_info=e)
        return False, EXPECTED_TABLES.copy()
    finally:
        if should_close and conn is not None:
            DatabaseManager.close_read_connection()
