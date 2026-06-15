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

log = get_dual_logger(__name__)

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
                    sf_ddl = BackupSchemaRegistry.get_snowflake_ddl(t_name)
                    conn.execute(text(sf_ddl))
                    log.dual_log(
                        tag="Backup:Cloud:Schema",
                        message=f"Created missing table {t_name}",
                        level="INFO",
                        payload={"table": t_name},
                    )
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
            c_type = "VECTOR(FLOAT, 1024)"
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

                mismatches = []
                pk_column = "id"
                for col in desired_cols:
                    c_name = col.name.lower()
                    if col.pk > 0:
                        pk_column = col.name
                    expected_sf_type = sqlite_type_to_snowflake(col.type)
                    actual_sf_type = existing[t_name].get(c_name, "").upper()
                    if actual_sf_type and expected_sf_type.upper() != actual_sf_type:
                        if {expected_sf_type.upper(), actual_sf_type} <= {"VARCHAR", "TEXT"}:
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
                    plans.append(TypeMismatchPlan(
                        table_name=t_name,
                        mismatches=mismatches,
                        clone_table_name=f"_migrate_{t_name}_{timestamp}",
                        new_ddl=BackupSchemaRegistry.get_snowflake_ddl(t_name),
                        columns_to_skip=[m.column_name for m in mismatches],
                        pk_column=pk_column,
                        is_master=True,
                    ))
        return plans

    @staticmethod
    def rebuild_table(engine, schema_name: str, plan, op_db_path: str):
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {schema_name}.{plan.table_name}"))
            conn.execute(text(plan.new_ddl))
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
                        if len(blob) == 4096:
                            float_list = list(struct.unpack('<1024f', blob))
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
                            select_expressions.append("PARSE_JSON(embedding)::VECTOR(FLOAT, 1024) as embedding")
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

            log.dual_log(tag="Migration:Cloud:Repopulate", level="INFO", message=f"Repopulated {total_inserted} rows to Snowflake {plan.table_name}", payload={"table": plan.table_name})
            return total_inserted
        finally:
            local_conn.close()
