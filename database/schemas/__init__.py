# database/schemas/__init__.py

import re
from typing import Dict, Optional
from database.connection import SQLITE_VEC_AVAILABLE
from database.schemas import jobs, vector, logs, stock_notes, sync_audit

# RULE: PERSISTED_TABLES must be an ordered list (parents before children) for FK-safe restores.
# RULE: Derived/External FTS tables (e.g., scraped_articles_fts) must NEVER be included here.
# They cannot be restored directly and must be rebuilt post-restoration.
# CRITICAL: ONLY tables explicitly listed in PERSISTED_TABLES are backed up.
# Ephemeral tables (like `jobs`, `job_items`) and physical artifacts (JSON, Markdown in `artifacts/`)
# are STRICTLY EXCLUDED and will not survive a cloud restore.
PERSISTED_TABLES: list[str] = [
    "scraped_articles",
    "scraped_articles_vec_backup",
    "broadcast_batches",
    "broadcast_details",
    "sn_filings",
    "sn_notes",
    "sn_detail_registry",
    "sn_note_details",
]

ALL_FTS_TABLES: Dict[str, str] = {
    **vector.FTS_TABLES,
}

ALL_TABLES: Dict[str, str] = {
    **jobs.TABLES, **vector.TABLES, **stock_notes.TABLES, **sync_audit.TABLES
}

ALL_VEC_TABLES: Dict[str, str] = {
    **jobs.VEC_TABLES, **vector.VEC_TABLES
}

ALL_TRIGGERS: Dict[str, str] = {
    **vector.TRIGGERS
}

# Logs tables are separate
LOGS_TABLES: Dict[str, str] = {
    **logs.LOGS_TABLES
}

def get_init_script() -> str:
    """Build the canonical init script for the main operational database."""
    parts = []
    for name, ddl in ALL_TABLES.items():
        parts.append(ddl)
    for name, ddl in ALL_FTS_TABLES.items():
        parts.append(ddl)
    for name, ddl in ALL_VEC_TABLES.items():
        if SQLITE_VEC_AVAILABLE:
            parts.append(ddl)
        else:
            parts.append(
                f"CREATE TABLE IF NOT EXISTS {name} "
                f"(rowid INTEGER PRIMARY KEY AUTOINCREMENT, embedding BLOB);"
            )
    for name, ddl in ALL_TRIGGERS.items():
        parts.append(ddl)
    return "\n".join(parts)

def get_logs_init_script() -> str:
    """Build the canonical init script for the logs database."""
    return "\n".join(LOGS_TABLES.values())

def get_repair_script(table_name: str) -> str:
    """Return repair DDL for a single table or trigger with vec0 fallback."""
    if table_name in ALL_TABLES:
        return ALL_TABLES[table_name]
    if table_name in ALL_FTS_TABLES:
        return ALL_FTS_TABLES[table_name]
    if table_name in ALL_VEC_TABLES:
        if SQLITE_VEC_AVAILABLE:
            return ALL_VEC_TABLES[table_name]
        return (
            f"CREATE TABLE IF NOT EXISTS {table_name} "
            f"(rowid INTEGER PRIMARY KEY AUTOINCREMENT, embedding BLOB);"
        )
    if table_name in ALL_TRIGGERS:
        return ALL_TRIGGERS[table_name]
    # LOGS_TABLES intentionally excluded - main DB writer never touches logs.db
    return ""
