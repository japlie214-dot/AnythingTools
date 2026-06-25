# database/backup/staging.py
"""Staging isolation for Snowflake writes.

When DATABASE_STAGING_ENABLED is true, all Snowflake operations target
<table_name>_staging instead of <table_name>. This module provides:

1. staging_table_name() — pure function, called at every SQL construction site.
2. StagingWipeService — startup/shutdown service that TRUNCATES staging tables.

Ref: Snowflake TRUNCATE TABLE — https://docs.snowflake.com/en/sql-reference/sql/truncate-table
Requires OWNERSHIP on the table or DELETE privilege.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


def staging_table_name(name: str) -> str:
    """Append _staging suffix when DATABASE_STAGING_ENABLED is true.

    Idempotent: if the name already ends in _staging, return as-is.
    This prevents double-suffixing when staging_table_name is called
    multiple times on the same name (e.g., in reconcile + cloud_writer).

    Reads the config at call time (not import time) to support tests
    that flip the flag.
    """
    from config import DATABASE_STAGING_ENABLED
    if not DATABASE_STAGING_ENABLED:
        return name
    if name.endswith("_staging"):
        return name
    return f"{name}_staging"


class StagingWipeService:
    """Wipe staging tables on startup and shutdown.

    Both methods are best-effort: they log WARNING on failure and continue.
    Never raise into the caller (startup orchestrator or app lifespan).
    """

    # Tables that have Snowflake counterparts. Used to derive the TRUNCATE list.
    # This list must match BackupSchemaRegistry.get_expected_sqlite_tables().
    # We import it lazily to avoid circular imports.
    PERSISTED_TABLES = [
        "sn_filings", "sn_notes", "sn_note_details", "sn_detail_registry",
        "sf_quarterly_facts", "sf_tickers",
        "scraped_articles", "scraped_articles_vec_backup",
        "broadcast_batches", "broadcast_details",
    ]

    @staticmethod
    def wipe_sqlite() -> dict[str, int]:
        """DELETE all rows from SQLite staging tables.

        Returns {table_name: rows_deleted}. Failures are logged WARNING.
        """
        from database.connection import get_staging_write_connection
        from config import DATABASE_STAGING_ENABLED

        if not DATABASE_STAGING_ENABLED:
            return {}

        results: dict[str, int] = {}
        try:
            conn = get_staging_write_connection()
        except Exception as e:
            log.dual_log(
                tag="Staging:Wipe:SQLite:Failed",
                message=f"Could not open staging SQLite connection: {e}",
                level="WARNING",
                payload={"error": str(e)},
            )
            return results

        try:
            for table in StagingWipeService.PERSISTED_TABLES:
                try:
                    cursor = conn.execute(f"DELETE FROM {table}")
                    deleted = cursor.rowcount
                    conn.commit()
                    results[table] = deleted
                    log.dual_log(
                        tag="Staging:Wipe:SQLite",
                        message=f"Wiped {table}: {deleted} rows deleted",
                        level="INFO",
                        payload={"table": table, "rows_deleted": deleted},
                    )
                except Exception as e:
                    results[table] = -1
                    log.dual_log(
                        tag="Staging:Wipe:SQLite:TableFailed",
                        message=f"Failed to wipe {table}: {e}",
                        level="WARNING",
                        payload={"table": table, "error": str(e)},
                    )
        finally:
            conn.close()

        return results

    @staticmethod
    def wipe_snowflake(cloud_engine: Any) -> dict[str, str]:
        """TRUNCATE all _staging-suffixed Snowflake tables.

        Returns {table_name: "ok"|"skipped"|"error:..."}.  Failures are
        logged WARNING and do not block other tables.

        Ref: https://docs.snowflake.com/en/sql-reference/sql/truncate-table
        """
        from config import DATABASE_STAGING_ENABLED

        if not DATABASE_STAGING_ENABLED:
            return {}

        results: dict[str, str] = {}
        engine = getattr(cloud_engine, "engine", None)
        if engine is None:
            log.dual_log(
                tag="Staging:Wipe:Snowflake:NoEngine",
                message="CloudEngine not initialized — skipping Snowflake staging wipe",
                level="WARNING",
            )
            return {"_all": "skipped:no_engine"}

        from sqlalchemy import text
        for table in StagingWipeService.PERSISTED_TABLES:
            staging_name = staging_table_name(table)
            # Defense-in-depth: only TRUNCATE tables ending in _staging
            if not staging_name.endswith("_staging"):
                log.dual_log(
                    tag="Staging:Wipe:Snowflake:Guard",
                    message=f"Refusing to TRUNCATE non-staging table: {staging_name}",
                    level="WARNING",
                    payload={"table": staging_name},
                )
                results[staging_name] = "skipped:guard"
                continue

            try:
                with engine.begin() as conn:
                    conn.execute(text(f"TRUNCATE TABLE {staging_name}"))
                results[staging_name] = "ok"
                log.dual_log(
                    tag="Staging:Wipe:Snowflake",
                    message=f"Truncated {staging_name}",
                    level="INFO",
                    payload={"table": staging_name},
                )
            except Exception as e:
                results[staging_name] = f"error:{e}"
                log.dual_log(
                    tag="Staging:Wipe:Snowflake:TableFailed",
                    message=f"Failed to truncate {staging_name}: {e}",
                    level="WARNING",
                    payload={"table": staging_name, "error": str(e)[:500]},
                )

        return results
