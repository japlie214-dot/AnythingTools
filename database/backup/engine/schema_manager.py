# database/backup/engine/schema_manager.py
import sqlite3
import time
from sqlalchemy import text
from database.backup.schema_registry import BackupSchemaRegistry
from database.management.migration_types import TypeMismatchPlan, ColumnMismatch
from database.management.schema_introspector import (
    sqlite_type_to_snowflake,
    _columns_from_ddl_in_memory,
    ColumnInfo,
)
from utils.logger import get_dual_logger
from database.backup.engine.type_normalizer import types_match, normalize_snowflake_type

log = get_dual_logger(__name__)

from database.backup.settings import Vec0BackupSettings as _Vec0Settings
_vec0_settings = _Vec0Settings()

class SnowflakeSchemaManager:
    @staticmethod
    def reconcile(engine, schema_name: str):
        with engine.begin() as conn:
            res = conn.execute(text("""
                SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = :schema
            """).bindparams(schema=schema_name.upper()))
            existing = {}
            for row in res:
                t_name, c_name, d_type = row[0].lower(), row[1].lower(), row[2]
                existing.setdefault(t_name, {})[c_name] = d_type

            expected_tables = BackupSchemaRegistry.get_expected_sqlite_tables()
            for t_name, ddl in expected_tables.items():
                if t_name not in existing:
                    # Per Pushback 2: get_snowflake_ddl may raise RuntimeError
                    # if the sqlglot AST rewrite fails. We must NOT let one bad
                    # table abort the entire reconcile loop — the app would fail
                    # to start. Wrap each table's DDL generation in try/except,
                    # log CRITICAL, and continue to the next table.
                    #
                    # Per FastAPI lifespan docs:
                    # https://fastapi.tiangolo.com/advanced/events/
                    # "Starlette will not start serving any incoming requests
                    # until the lifespan has been run." An unhandled exception
                    # in startup prevents serving.
                    try:
                        sf_ddl = BackupSchemaRegistry.get_snowflake_ddl(t_name)
                        conn.execute(text(sf_ddl))
                        log.dual_log(
                            tag="Backup:Cloud:Schema",
                            message=f"Created missing table {t_name}",
                            level="INFO",
                            payload={"table": t_name},
                        )
                    except RuntimeError as e:
                        # DDL generation failed for this table. The
                        # BackupSchemaRegistry already logged a CRITICAL with
                        # full diagnostic context. Log a per-table ERROR here
                        # so operators can see which table in the reconcile
                        # loop was skipped, then continue.
                        log.dual_log(
                            tag="Backup:Cloud:Schema:Skipped",
                            level="ERROR",
                            message=f"Skipping table {t_name} during reconcile: DDL generation failed",
                            payload={
                                "table": t_name,
                                "error": str(e)[:1000],
                                "error_type": type(e).__name__,
                                "impact": "Table will not be created in Snowflake; cloud sync for this table is degraded until the AST rewrite is fixed.",
                            },
                        )
                        continue
                else:
                    SnowflakeSchemaManager._reconcile_columns(
                        conn, t_name, ddl, existing[t_name]
                    )

    @staticmethod
    def _reconcile_columns(conn, t_name: str, ddl: str, existing_cols: dict):
        sqlite_cols = _columns_from_ddl_in_memory(ddl, t_name)
        if sqlite_cols is None:
            if "vec0" in ddl.lower():
                sqlite_cols = [
                    ColumnInfo(cid=0, name='rowid', type='INTEGER', notnull=False, dflt_value=None, pk=1),
                    ColumnInfo(cid=1, name='embedding', type='BLOB', notnull=False, dflt_value=None, pk=0),
                ]
            else:
                return

        desired_col_names = set()
        for col in sqlite_cols:
            c_name = col.name.lower()
            desired_col_names.add(c_name)
            if c_name not in existing_cols:
                SnowflakeSchemaManager._add_column(conn, t_name, c_name, col.type.upper())

        cloud_col_names = set(existing_cols.keys())
        extra_cols = cloud_col_names - desired_col_names
        for c_name in extra_cols:
            try:
                conn.execute(text(f"ALTER TABLE {t_name} DROP COLUMN {c_name}"))
                log.dual_log(tag="Backup:Cloud:DropColumn", level="WARNING", message=f"Dropped unexpected column {c_name} from {t_name}", payload={"table": t_name, "column": c_name})
            except Exception as e:
                log.dual_log(tag="Backup:Cloud:DropColumnFailed", level="WARNING", message=f"Failed to drop column {c_name} from {t_name}: {e}", payload={"table": t_name, "column": c_name, "error": str(e)})

    @staticmethod
    def _add_column(conn, t_name: str, c_name: str, sqlite_type: str):
        if c_name == "embedding":
            c_type = f"VECTOR(FLOAT, {_vec0_settings.dim})"
        elif "TEXT" in sqlite_type or "CHAR" in sqlite_type:
            c_type = "VARCHAR"
        elif "REAL" in sqlite_type or "FLOA" in sqlite_type:
            c_type = "FLOAT"
        elif "BLOB" in sqlite_type:
            c_type = "BINARY"
        else:
            c_type = "NUMBER"
        conn.execute(text(f"ALTER TABLE {t_name} ADD COLUMN {c_name} {c_type}"))
        log.dual_log(tag="Backup:Cloud:Schema", message=f"Added column {c_name} to {t_name}", level="INFO", payload={"table": t_name, "column": c_name})

    @staticmethod
    def reconcile_types(engine, schema_name: str) -> list:
        """Detect type mismatches between the local SQLite schema and the
        Snowflake cloud schema.

        Compares the actual Snowflake column type (from
        INFORMATION_SCHEMA.COLUMNS.DATA_TYPE) against the expected type
        (from BackupSchemaRegistry.expected_snowflake_types, which consults
        SNOWFLAKE_COLUMN_OVERRIDES).

        Type comparison uses types_match() from type_normalizer.py which
        normalizes both sides to the base type name (e.g.
        "VECTOR(FLOAT, 1024)" → "VECTOR"). This is necessary because
        Snowflake's INFORMATION_SCHEMA.COLUMNS.DATA_TYPE returns only the
        base type for parameterized types, per the docs:
        https://docs.snowflake.com/en/sql-reference/data-types-structured
        "For columns of structured types, the INFORMATION_SCHEMA COLUMNS
         view only provides information about the basic data type of the
         column (ARRAY, OBJECT, or MAP)."

        Without normalization, override-registered columns (e.g.
        scraped_articles_vec_backup.embedding VECTOR(FLOAT, 1024)) would
        be flagged as mismatches on every startup ("VECTOR(FLOAT, 1024)"
        != "VECTOR"), triggering spurious table rebuilds that re-push
        hundreds of rows of embeddings each time.
        """
        plans = []
        with engine.begin() as conn:
            res = conn.execute(text("""
                SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = :schema
            """).bindparams(schema=schema_name.upper()))
            existing = {}
            for row in res:
                t_name, c_name, d_type = row[0].lower(), row[1].lower(), row[2]
                existing.setdefault(t_name, {})[c_name] = d_type.upper()

            expected_tables = BackupSchemaRegistry.get_expected_sqlite_tables()
            for t_name, ddl in expected_tables.items():
                if t_name not in existing:
                    continue
                desired_cols = _columns_from_ddl_in_memory(ddl, t_name)
                if not desired_cols:
                    continue

                # Pre-compute the override-aware expected types for this table.
                # BackupSchemaRegistry.expected_snowflake_types() consults the
                # SNOWFLAKE_COLUMN_OVERRIDES registry (defined in
                # database/schemas/_snowflake_overrides.py), so columns like
                # scraped_articles_vec_backup.embedding (BLOB in SQLite,
                # VECTOR(FLOAT, 1024) in Snowflake) are correctly recognized
                # as matching and NOT flagged for rebuild.
                expected_sf_types_for_table = BackupSchemaRegistry.expected_snowflake_types(t_name)

                mismatches = []
                pk_column = "id"
                for col in desired_cols:
                    c_name = col.name.lower()
                    if col.pk > 0:
                        pk_column = col.name
                    # Look up the expected Snowflake type from the override-aware
                    # registry. Fall back to the generic transpiler only if the
                    # column is not present (which would be a programming error
                    # since expected_snowflake_types covers all columns in the DDL).
                    expected_sf_type = expected_sf_types_for_table.get(
                        c_name,
                        sqlite_type_to_snowflake(col.type),
                    )
                    actual_sf_type = existing[t_name].get(c_name, "").upper()
                    # Use types_match() which normalizes both sides to the
                    # base type name, so VECTOR(FLOAT, 1024) matches VECTOR.
                    # Ref: https://docs.snowflake.com/en/sql-reference/data-types-structured
                    if actual_sf_type and not types_match(expected_sf_type, actual_sf_type):
                        # Backward-compat: VARCHAR and TEXT are semantically
                        # equivalent in Snowflake (both are variable-length
                        # strings). Ref: https://docs.snowflake.com/en/sql-reference/data-types-text
                        # Normalize both and check if they're in the VARCHAR/TEXT family.
                        norm_expected = normalize_snowflake_type(expected_sf_type)
                        norm_actual = normalize_snowflake_type(actual_sf_type)
                        if {norm_expected, norm_actual} <= {"VARCHAR", "TEXT"}:
                            continue
                        is_pk = col.pk > 0
                        mismatches.append(ColumnMismatch(
                            column_name=c_name,
                            actual_type=actual_sf_type,
                            expected_type=expected_sf_type.upper(),
                            is_primary_key=is_pk,
                        ))

                if mismatches:
                    timestamp = int(time.time())
                    # Log the WHY: column-level mismatch details before
                    # rebuilding. Without this, operators see "Recreated
                    # table X" but never see "because column Y drifted
                    # from A to B". The payload includes the full mismatch
                    # list and a preview of the new DDL.
                    log.dual_log(
                        tag="Migration:Cloud:RebuildContext",
                        level="WARNING",
                        message=f"Type mismatch detected for {t_name}: {len(mismatches)} column(s)",
                        payload={
                            "table": t_name,
                            "trigger": "type_mismatch",
                            "mismatches": [
                                {
                                    "column": m.column_name,
                                    "actual_type": m.actual_type,
                                    "expected_type": m.expected_type,
                                    "is_primary_key": m.is_primary_key,
                                }
                                for m in mismatches
                            ],
                            "new_ddl_preview": SnowflakeSchemaManager._safe_get_snowflake_ddl_preview(t_name),
                         },
                    )
                    # Per Pushback 2: wrap the DDL generation in try/except so
                    # a single bad table does not abort reconcile_types.
                    try:
                        new_ddl = BackupSchemaRegistry.get_snowflake_ddl(t_name)
                    except RuntimeError as e:
                        log.dual_log(
                            tag="Backup:Cloud:Schema:Skipped",
                            level="ERROR",
                            message=f"Skipping type-mismatch rebuild for {t_name}: DDL generation failed",
                            payload={
                                "table": t_name,
                                "error": str(e)[:1000],
                                "error_type": type(e).__name__,
                                "impact": "Table will retain its current Snowflake schema; type mismatch will be re-detected on next startup.",
                            },
                        )
                        continue
                    plans.append(TypeMismatchPlan(
                        table_name=t_name,
                        mismatches=mismatches,
                        clone_table_name=f"_migrate_{t_name}_{timestamp}",
                        new_ddl=new_ddl,
                        columns_to_skip=[m.column_name for m in mismatches],
                        pk_column=pk_column,
                        is_master=True,
                    ))
        return plans

    @staticmethod
    def _safe_get_snowflake_ddl_preview(table_name: str) -> str:
        """Get a DDL preview for logging, returning an error marker on failure.

        Used in logging contexts where a DDL generation failure should not
        abort the surrounding log emission.
        """
        try:
            return BackupSchemaRegistry.get_snowflake_ddl(table_name)[:500]
        except Exception as e:
            return f"[DDL_GENERATION_FAILED: {type(e).__name__}: {str(e)[:200]}]"

    @staticmethod
    def rebuild_table(engine, schema_name: str, plan, op_db_path: str):
        """Rebuild a Snowflake table: DROP + CREATE + repopulate from SQLite.

        Logs the WHY (mismatch details) before rebuilding and the row-count
        comparison after repopulating. If the CREATE TABLE DDL fails, logs
        the full DDL and error so operators can diagnose without re-running.
        """
        rebuild_start = time.time()
        # Query local row count BEFORE dropping (for post-rebuild comparison).
        # This catches silent data loss if repopulate inserts fewer rows
        # than expected (e.g. due to a concurrent delete or a chunking bug).
        local_row_count = 0
        try:
            local_conn_probe = sqlite3.connect(op_db_path, timeout=30.0)
            try:
                local_row_count = local_conn_probe.execute(
                    f"SELECT COUNT(*) FROM {plan.table_name}"
                ).fetchone()[0]
            finally:
                local_conn_probe.close()
        except Exception as e:
            log.dual_log(
                tag="Migration:Cloud:RebuildContext",
                level="WARNING",
                message=f"Could not pre-count rows for {plan.table_name}: {e}",
                payload={"table": plan.table_name, "error": str(e)[:200]},
            )

        with engine.begin() as conn:
            try:
                conn.execute(text(f"DROP TABLE IF EXISTS {schema_name}.{plan.table_name}"))
                conn.execute(text(plan.new_ddl))
            except Exception as ddl_err:
                # Log the full DDL that failed so operators can diagnose
                # without re-running. This is critical for cases like the
                # IS_TOTAL bug where the DDL has an incompatible DEFAULT
                # clause — the error message alone ("002262: Default value
                # data type does not match") is useless without the DDL.
                log.dual_log(
                    tag="Migration:Cloud:RebuildFailed",
                    level="ERROR",
                    message=f"DDL failed for {plan.table_name}: {ddl_err}",
                    payload={
                        "table": plan.table_name,
                        "trigger": "ddl_failure",
                        "error": str(ddl_err)[:1000],
                        "failed_ddl": plan.new_ddl[:2000],
                        "mismatches": [
                            {"column": m.column_name, "actual": m.actual_type, "expected": m.expected_type}
                            for m in plan.mismatches
                        ],
                    },
                )
                raise
            log.dual_log(tag="Migration:Cloud:Recreate", level="INFO", message=f"Recreated Snowflake table {plan.table_name}", payload={"table": plan.table_name})

        local_conn = sqlite3.connect(op_db_path, timeout=30.0)
        try:
            cursor = local_conn.execute(f"PRAGMA table_info({plan.table_name})")
            insert_cols = [row[1] for row in cursor.fetchall()]

            if not insert_cols:
                return 0

            batch_size = 1000
            total_inserted = 0
            offset = 0

            while True:
                rows = local_conn.execute(
                    f"SELECT {','.join(insert_cols)} FROM {plan.table_name} LIMIT {batch_size} OFFSET {offset}"
                ).fetchall()
                if not rows:
                    break

                dict_rows = []
                for r in rows:
                    row_dict = dict(zip(insert_cols, r))
                    if "embedding" in row_dict and isinstance(row_dict["embedding"], bytes):
                        import struct
                        import json
                        blob = row_dict["embedding"]
                        expected_bytes = _vec0_settings.dim * 4  # float32 = 4 bytes
                        if len(blob) == expected_bytes:
                            float_list = list(struct.unpack(f'<{_vec0_settings.dim}f', blob))
                            row_dict["embedding"] = json.dumps(float_list)
                    dict_rows.append(row_dict)

                from database.backup.engine.type_sanitizer import sanitize_snowflake_params
                dict_rows = sanitize_snowflake_params(dict_rows)

                with engine.begin() as conn:
                    stage_table = f"{plan.table_name}_stage"
                    expected_types = BackupSchemaRegistry.expected_snowflake_types(plan.table_name)

                    stage_cols = []
                    for c in insert_cols:
                        if c == "embedding":
                            stage_cols.append(f"{c} VARCHAR")
                        else:
                            stage_cols.append(f"{c} {expected_types.get(c.lower(), 'VARCHAR')}")

                    stage_ddl = f"CREATE OR REPLACE TEMPORARY TABLE {schema_name}.{stage_table} ({', '.join(stage_cols)})"
                    conn.execute(text(stage_ddl))

                    col_placeholders = ", ".join([f":{c}" for c in insert_cols])
                    col_names = ", ".join(insert_cols)
                    insert_sql = f"INSERT INTO {schema_name}.{stage_table} ({col_names}) VALUES ({col_placeholders})"
                    conn.execute(text(insert_sql), dict_rows)

                    select_expressions = []
                    for c in insert_cols:
                        if c == "embedding":
                            select_expressions.append(f"PARSE_JSON(embedding)::VECTOR(FLOAT, {_vec0_settings.dim}) as embedding")
                        else:
                            select_expressions.append(c)

                    repopulate_sql = f"""
                        INSERT INTO {schema_name}.{plan.table_name} ({col_names})
                        SELECT {', '.join(select_expressions)}
                        FROM {schema_name}.{stage_table}
                    """
                    conn.execute(text(repopulate_sql))

                total_inserted += len(rows)
                offset += batch_size

            # Log row-count comparison: if total_inserted != local_row_count,
            # data was silently lost. This is a WARNING, not an ERROR, because
            # the table may have been legitimately smaller than expected
            # (e.g. concurrent deletes during rebuild). But the operator
            # MUST be alerted to investigate.
            row_count_match = (total_inserted == local_row_count)
            log.dual_log(
                tag="Migration:Cloud:Repopulate",
                level="INFO" if row_count_match else "WARNING",
                message=f"Repopulate complete for {plan.table_name}: inserted={total_inserted}, expected={local_row_count}",
                payload={
                    "table": plan.table_name,
                    "rows_inserted": total_inserted,
                    "rows_expected": local_row_count,
                    "row_count_match": row_count_match,
                    "duration_seconds": round(time.time() - rebuild_start, 2),
                },
            )
            if not row_count_match:
                log.dual_log(
                    tag="Migration:Cloud:RowCountMismatch",
                    level="WARNING",
                    message=f"Row count mismatch for {plan.table_name}: inserted {total_inserted} but expected {local_row_count}. Possible data loss.",
                    payload={
                        "table": plan.table_name,
                        "inserted": total_inserted,
                        "expected": local_row_count,
                        "delta": local_row_count - total_inserted,
                    },
                )
            return total_inserted
        finally:
            local_conn.close()
