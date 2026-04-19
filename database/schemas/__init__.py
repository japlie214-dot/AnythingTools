# database/schemas/__init__.py

import re
from typing import Dict, Optional
from database.connection import SQLITE_VEC_AVAILABLE
from database.schemas import jobs, finance, vector, pdf, token

BASE_SCHEMA_VERSION = 3
MAX_MIGRATION_SCRIPTS = 3

ALL_TABLES: Dict[str, str] = {
    **jobs.TABLES, **finance.TABLES, **vector.TABLES, **pdf.TABLES, **token.TABLES
}
ALL_VEC_TABLES: Dict[str, str] = {
    **jobs.VEC_TABLES, **finance.VEC_TABLES, **vector.VEC_TABLES, **pdf.VEC_TABLES, **token.VEC_TABLES
}

def get_init_script() -> str:
    """Build the canonical init script from all domain modules."""
    parts = []
    for name, ddl in ALL_TABLES.items():
        parts.append(ddl)
    for name, ddl in ALL_VEC_TABLES.items():
        if SQLITE_VEC_AVAILABLE:
            parts.append(ddl)
        else:
            parts.append(
                f"CREATE TABLE IF NOT EXISTS {name} "
                f"(rowid INTEGER PRIMARY KEY AUTOINCREMENT, embedding BLOB);"
            )
    return "\n".join(parts)

def get_repair_script(table_name: str) -> Optional[str]:
    """Return repair DDL for a single table with vec0 fallback."""
    if table_name in ALL_TABLES:
        return ALL_TABLES[table_name]
    if table_name in ALL_VEC_TABLES:
        if SQLITE_VEC_AVAILABLE:
            return ALL_VEC_TABLES[table_name]
        return (
            f"CREATE TABLE IF NOT EXISTS {table_name} "
            f"(rowid INTEGER PRIMARY KEY AUTOINCREMENT, embedding BLOB);"
        )
    return None
