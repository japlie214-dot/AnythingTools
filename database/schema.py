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
    """DEPRECATED: Dynamic version check to avoid side-effects during import."""
    log.dual_log(tag="DB:Schema", level="WARNING", message="get_schema_version() is deprecated")
    from database.migrations import get_latest_version
    return get_latest_version()

def get_init_script() -> str:
    return _get_init_script()

def get_repair_script(table_name: str) -> str | None:
    return _get_repair_script(table_name)
