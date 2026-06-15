# database/backup/engine/sync_operations.py
import datetime
import struct
from decimal import Decimal
from typing import List

from sqlalchemy import text
from database.backup.engine.type_sanitizer import sanitize_snowflake_params
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def upload_local_manifest(cloud_engine, local_conn, cloud_conn, table_name: str, pk_col: str, hash_col: str) -> str:
    manifest_table = f"{table_name}_manifest_tmp"
    cloud_conn.execute(text(
        f"CREATE OR REPLACE TEMPORARY TABLE {cloud_engine.settings.schema_name}.{manifest_table} "
        f"(id VARCHAR, content_hash VARCHAR)"
    ))
    try:
        cursor = local_conn.execute(f"SELECT {pk_col}, {hash_col} FROM {table_name}")
        manifest_rows = [{"id": str(row[0]), "content_hash": str(row[1])} for row in cursor.fetchall()]
    except Exception:
        manifest_rows = []
    if manifest_rows:
        batch_size = 10000
        for i in range(0, len(manifest_rows), batch_size):
            batch = manifest_rows[i : i + batch_size]
            cloud_conn.execute(text(f"INSERT INTO {cloud_engine.settings.schema_name}.{manifest_table} (id, content_hash) VALUES (:id, :content_hash)"), batch)
    return manifest_table

def merge_to_cloud(cloud_conn, schema: str, table_name: str, columns: List[str], dict_rows: list, pk_col) -> int:
    dict_rows = sanitize_snowflake_params(dict_rows)
    stage_table = f"{table_name}_stage"

    from database.backup.schema_registry import BackupSchemaRegistry
    expected_types = BackupSchemaRegistry.expected_snowflake_types(table_name)

    pk_cols = [pk_col] if isinstance(pk_col, str) else list(pk_col)
    col_defs = ",".join([f"{c} {expected_types.get(c.lower(), 'VARCHAR')}" for c in columns])
    cloud_conn.execute(text(f"CREATE OR REPLACE TEMPORARY TABLE {schema}.{stage_table} ({col_defs})"))

    insert_placeholders = ",".join([f":{c}" for c in columns])
    cloud_conn.execute(text(f"INSERT INTO {schema}.{stage_table} VALUES ({insert_placeholders})"), dict_rows)

    merge_on = " AND ".join([f"t.{c} = s.{c}" for c in pk_cols])
    update_set = ", ".join([f"t.{c} = s.{c}" for c in columns if c not in pk_cols])

    merge_sql = f"""
    MERGE INTO {schema}.{table_name} t
    USING {schema}.{stage_table} s
    ON {merge_on}
    WHEN MATCHED THEN UPDATE SET {update_set}
    WHEN NOT MATCHED THEN INSERT ({",".join(columns)}) VALUES ({",".join([f"s.{c}" for c in columns])})
    """
    result = cloud_conn.execute(text(merge_sql))
    return result.rowcount if hasattr(result, "rowcount") else len(dict_rows)

def sync_data(cloud_engine, local_db_path: str, tables: dict, batch_size: int = 500, delta_only: bool = True) -> dict:
    import sqlite3

    if not cloud_engine.settings.enabled or not cloud_engine.engine:
        return {"status": "disabled"}

    def _execute_cloud_sync():
        local_conn = None
        results = {}
        try:
            local_conn = sqlite3.connect(local_db_path, timeout=30.0)
            with cloud_engine.engine.begin() as cloud_conn:
                for table_name in tables:
                    if "VIRTUAL" in tables[table_name].upper():
                        continue
                    pk_col = "id"
                    hash_col = "content_hash"
                    has_hash = False
                    try:
                        for col_info in local_conn.execute(f"PRAGMA table_info({table_name})").fetchall():
                            if col_info[5] > 0: pk_col = col_info[1]
                            if col_info[1] == "content_hash": has_hash = True
                        if not has_hash: hash_col = "''"
                    except Exception:
                        pass

                    manifest_table = upload_local_manifest(cloud_engine, local_conn, cloud_conn, table_name, pk_col, hash_col)

                    try:
                        diff_query = f"""
                        SELECT m.id FROM {cloud_engine.settings.schema_name}.{manifest_table} m
                        LEFT JOIN {cloud_engine.settings.schema_name}.{table_name} c ON m.id = c.{pk_col}
                        WHERE c.{pk_col} IS NULL
                        """
                        if has_hash:
                            diff_query += " OR COALESCE(m.content_hash, '') != COALESCE(c.content_hash, '')"
                        cloud_needs = cloud_conn.execute(text(diff_query)).fetchall()
                        ids_to_push = [r[0] for r in cloud_needs]
                    except Exception as e:
                        log.dual_log(tag="Backup:Cloud:DiffFail", message=f"Diff query failed for {table_name}: {e}", level="WARNING", payload={"error": str(e)})
                        cursor = local_conn.execute(f"SELECT {pk_col} FROM {table_name}")
                        ids_to_push = [r[0] for r in cursor.fetchall()]

                    if not ids_to_push:
                        results[table_name] = 0
                        continue

                    chunk_size = 900
                    dict_rows = []
                    columns = []
                    for i in range(0, len(ids_to_push), chunk_size):
                        chunk = ids_to_push[i : i + chunk_size]
                        placeholders = ",".join("?" for _ in chunk)
                        cursor = local_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} IN ({placeholders})", chunk)
                        if not columns and cursor.description:
                            columns = [desc[0] for desc in cursor.description]
                        dict_rows.extend([dict(zip(columns, r)) for r in cursor.fetchall()])

                    if not dict_rows or not columns:
                        results[table_name] = 0
                        continue

                    if "embedding" in columns:
                        if cloud_engine.vec0_settings.use_native_vector_type:
                            from database.backup.vec.cloud_vector_pusher import VectorSync
                            pusher = VectorSync(circuit_breaker=cloud_engine.circuit_breaker_vec)
                            push_result = pusher.push_vectors(cloud_conn, cloud_engine.settings.schema_name, table_name, columns, dict_rows, pk_col, batch_size=batch_size)
                            results[table_name] = push_result["pushed"]
                        else:
                            results[table_name] = merge_to_cloud(cloud_conn, cloud_engine.settings.schema_name, table_name, columns, dict_rows, pk_col)
                    else:
                        results[table_name] = merge_to_cloud(cloud_conn, cloud_engine.settings.schema_name, table_name, columns, dict_rows, pk_col)
                return results
        finally:
            if local_conn is not None:
                try:
                    local_conn.close()
                except Exception:
                    pass

    return cloud_engine.circuit_breaker_push.call(_execute_cloud_sync)

def pull_to_local(cloud_engine, local_db_path: str, tables: dict) -> dict:
    import sqlite3

    if not cloud_engine.settings.enabled or not cloud_engine.engine:
        return {"status": "disabled"}

    def _execute_pull():
        results = {}
        try:
            local_conn = sqlite3.connect(local_db_path, timeout=30.0)
            with cloud_engine.engine.begin() as cloud_conn:
                for table_name in tables:
                    if "VIRTUAL" in tables[table_name].upper():
                        continue
                    pk_col = "id"
                    hash_col = "content_hash"
                    has_hash = False
                    try:
                        for col_info in local_conn.execute(f"PRAGMA table_info({table_name})").fetchall():
                            if col_info[5] > 0: pk_col = col_info[1]
                            if col_info[1] == "content_hash": has_hash = True
                        if not has_hash: hash_col = "''"
                    except Exception:
                        pass

                    manifest_table = upload_local_manifest(cloud_engine, local_conn, cloud_conn, table_name, pk_col, hash_col)

                    diff_query = f"""
                        SELECT c.* FROM {cloud_engine.settings.schema_name}.{table_name} c
                        LEFT JOIN {cloud_engine.settings.schema_name}.{manifest_table} m
                            ON c.{pk_col} = m.id
                        WHERE m.id IS NULL
                    """
                    if has_hash:
                        diff_query += " OR COALESCE(c.content_hash, '') != COALESCE(m.content_hash, '')"
                    cloud_rows = cloud_conn.execute(text(diff_query)).fetchall()

                    if not cloud_rows:
                        results[table_name] = 0
                        continue

                    columns = [k.lower() for k in cloud_rows[0]._mapping.keys()]
                    has_embedding = "embedding" in columns

                    if has_embedding and cloud_engine.vec0_settings.use_native_vector_type:
                        from database.backup.vec.cloud_vector_pusher import VectorSync
                        vector_sync = VectorSync()
                        records, dlq_rows = vector_sync.pull_vectors_from_cloud(cloud_rows, columns)
                        if dlq_rows:
                            _route_dlq_rows(dlq_rows, table_name, pk_col)
                    else:
                        records = _normalize_cloud_rows(cloud_rows, columns)

                    if "content_hash" in columns:
                        for r in records:
                            if isinstance(r, dict) and not r.get("content_hash"):
                                from database.backup.sync.foundation import ContentHasher
                                new_hash = ContentHasher.compute_row_hash(table_name, r)
                                r["content_hash"] = new_hash

                    if records:
                        cols = list(records[0].keys())
                        placeholders = ",".join(["?"] * len(cols))
                        insert_sql = f"INSERT OR REPLACE INTO {table_name} ({','.join(cols)}) VALUES ({placeholders})"
                        rows_as_tuples = [tuple(r.get(c) for c in cols) for r in records]
                        local_conn.executemany(insert_sql, rows_as_tuples)
                        local_conn.commit()
                        log.dual_log(tag="Backup:Pull:Written", message=f"Wrote {len(records)} rows to local table {table_name}", level="INFO", payload={"table": table_name, "rows": len(records)})
                    results[table_name] = len(records)
        finally:
            local_conn.close()
        return results

    return cloud_engine.circuit_breaker_pull.call(_execute_pull)

def _normalize_cloud_rows(cloud_rows, columns: list) -> list:
    records = []
    for row in cloud_rows:
        norm_row = []
        for val in row:
            if isinstance(val, (datetime.datetime, datetime.date)):
                norm_row.append(val.isoformat())
            elif isinstance(val, Decimal):
                norm_row.append(float(val))
            elif isinstance(val, list) and len(val) > 0 and isinstance(val[0], float):
                norm_row.append(struct.pack(f"<{len(val)}f", *val))
            else:
                norm_row.append(val)
        records.append(dict(zip(columns, norm_row)))
    return records

def _route_dlq_rows(dlq_rows: list, table_name: str, pk_col: str):
    from database.writer import enqueue_write
    import json
    from utils.id_generator import ULID

    for dlq_row in dlq_rows:
        safe_dlq_row = {k: v for k, v in dlq_row.items() if k != "_error_msg" and not isinstance(v, bytes)}
        enqueue_write(
            "INSERT INTO dead_letter_queue (dlq_id, table_name, row_id, row_data, error_message) VALUES (?, ?, ?, ?, ?)",
            (ULID.generate(), table_name, dlq_row.get(pk_col, ""), json.dumps(safe_dlq_row), dlq_row.get("_error_msg")),
        )
