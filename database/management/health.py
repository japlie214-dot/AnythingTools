# database/management/health.py
"""Agnostic database health checks and recovery operations."""

import sqlite3
import shutil
from typing import List, Tuple, Optional
from pathlib import Path
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


def restore_orphaned_backup(db_path: Path) -> None:
    """Detect and restore orphaned backup files. Agnostic to file path."""
    backup_path = db_path.with_suffix(".db.bak")
    if not backup_path.exists():
        return
    
    if backup_path.stat().st_size == 0:
        log.dual_log(tag="DB:Health:Critical", level="CRITICAL",
                    message=f"Orphaned backup for {db_path.name} is 0 bytes. Halting.",
                    payload={"db_name": db_path.name, "backup_path": str(backup_path), "backup_size": 0, "issue": "zero_byte_backup"})
        raise RuntimeError(f"Orphaned backup for {db_path.name} is 0 bytes.")
    
    try:
        log.dual_log(tag="DB:Health:Warning", level="WARNING",
                    message=f"Orphaned backup detected for {db_path.name}. Restoring.",
                    payload={"db_name": db_path.name, "backup_path": str(backup_path), "backup_size": backup_path.stat().st_size})
        for suffix in ["-wal", "-shm"]:
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
        
        shutil.copy2(backup_path, db_path)
        backup_path.unlink()
        log.dual_log(tag="DB:Health:Success", level="INFO",
                    message=f"Orphaned backup for {db_path.name} restored successfully.",
                    payload={"db_name": db_path.name, "action": "orphaned_backup_restored"})
    except Exception as e:
        log.dual_log(tag="DB:Health:Error", level="CRITICAL",
                    message=f"Failed to restore orphaned backup for {db_path.name}: {e}",
                    payload={"db_name": db_path.name, "error": str(e), "error_type": type(e).__name__})
        raise RuntimeError(f"Failed to restore orphaned backup for {db_path.name}: {e}")


def check_database_file_state(db_path: Path) -> Tuple[bool, bool]:
    """Check database existence and corruption state.
    
    Args:
        db_path: Path to the database file
        
    Returns:
        Tuple[exists: bool, is_corrupted: bool]
    """
    if not db_path.exists():
        return False, False
    
    if db_path.stat().st_size == 0:
        return True, True
    
    try:
        # Use an isolated connection to prevent poisoning
        conn = sqlite3.connect(str(db_path))
        # Try a simple query to verify the DB is not corrupted
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        conn.close()
        return True, False
    except sqlite3.DatabaseError:
        return True, True
    except Exception:
        return True, True


def check_tables_exist(conn: sqlite3.Connection, expected_names: List[str]) -> Tuple[bool, List[str]]:
    """Agnostic check for table existence based on provided list."""
    existing = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = [t for t in expected_names if t not in existing]
    
    if missing:
        log.dual_log(tag="DB:Health:Warning", level="WARNING", message=f"Missing tables: {missing}",
                     payload={"missing_tables": missing, "expected_tables": expected_names, "found_count": len(existing)})
    else:
        log.dual_log(tag="DB:Health:Success", level="INFO", message="All expected tables verified.",
                     payload={"verified_count": len(expected_names)})
    
    return len(missing) == 0, missing
