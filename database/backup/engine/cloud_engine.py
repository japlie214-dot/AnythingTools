# database/backup/engine/cloud_engine.py
import struct
from typing import List
from sqlalchemy import create_engine, text
from database.backup.settings import CloudBackupSettings
from database.backup.schema_registry import BackupSchemaRegistry
from database.backup.resilience.circuit_breaker import CircuitBreaker
from database.backup.sync.helpers import introspect_table_columns, normalize_cloud_row
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class SnowflakeSchemaManager:
    @staticmethod
    def reconcile_non_destructive(engine, schema_name: str):
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
                    log.dual_log(tag="Backup:Cloud:Schema", message=f"Created missing table {t_name}", level="INFO", payload={"table": t_name})
                else:
                    import sqlite3
                    sqlite_cols = []
                    with sqlite3.connect(":memory:") as temp_db:
                        try:
                            temp_db.executescript(ddl)
                            sqlite_cols = temp_db.execute(f"PRAGMA table_info({t_name})").fetchall()
                        except sqlite3.OperationalError:
                            # Fallback for virtual tables that require unloaded extensions
                            if "vec0" in ddl.lower():
                                sqlite_cols = [(0, "rowid", "INTEGER", 0, None, 1), (1, "embedding", "BLOB", 0, None, 0)]
                    for col in sqlite_cols:
                        c_name = col[1].lower()
                        if c_name not in existing[t_name]:
                            c_type = "VARCHAR" if "TEXT" in col[2].upper() else "NUMBER"
                            if c_name == "embedding":
                                c_type = "VECTOR(FLOAT, 1024)"
                            conn.execute(text(f"ALTER TABLE {t_name} ADD COLUMN {c_name} {c_type}"))
                            log.dual_log(tag="Backup:Cloud:Schema", message=f"Added column {c_name} to {t_name}", level="INFO", payload={"table": t_name, "column": c_name})
                        elif c_name == "embedding":
                            existing_type = existing[t_name][c_name].upper()
                            if "VECTOR" not in existing_type:
                                log.dual_log(
                                    tag="Backup:Cloud:Schema:Migration",
                                    message=f"Column {c_name} in {t_name} is {existing_type}, expected VECTOR(FLOAT, 1024).",
                                    level="WARNING",
                                    payload={"table": t_name, "column": c_name}
                                )

from database.backup.engine.base import BackupEngine

class CloudEngine(BackupEngine):
    def __init__(self, settings: CloudBackupSettings, cb_settings):
        self.settings = settings
        # DILIGENCE: Load vec0 settings internally to preserve backward compatibility
        from database.backup.settings import Vec0BackupSettings
        self.vec0_settings = Vec0BackupSettings()

        if self.settings.enabled:
            url = f"snowflake://{settings.user}@{settings.account}/{settings.database}/{settings.schema_name}?warehouse={settings.warehouse}"
            self.engine = create_engine(
                url,
                connect_args={'private_key': self._load_private_key()},
                pool_size=settings.pool_size,
                max_overflow=settings.max_overflow,
                pool_pre_ping=True
            )
        else:
            self.engine = None

        # circuit breakers
        self.circuit_breaker_push = CircuitBreaker(
            failure_threshold=cb_settings.circuit_breaker_threshold,
            reset_timeout=cb_settings.circuit_breaker_reset_seconds
        )
        self.circuit_breaker_pull = CircuitBreaker(
            failure_threshold=cb_settings.circuit_breaker_threshold,
            reset_timeout=cb_settings.circuit_breaker_reset_seconds
        )
        self.circuit_breaker_vec = CircuitBreaker(
            failure_threshold=cb_settings.circuit_breaker_threshold,
            reset_timeout=cb_settings.circuit_breaker_reset_seconds
        )
        self.circuit_breaker = self.circuit_breaker_push

    def _load_private_key(self):
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        with open(self.settings.private_key_path, "rb") as kf:
            p_key = serialization.load_pem_private_key(kf.read(), password=None, backend=default_backend())
        return p_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )

    def startup(self) -> dict:
        if not self.settings.enabled:
            return {"status": "disabled"}
        def _do_startup():
            with self.engine.begin() as conn:
                conn.execute(text("SELECT CURRENT_VERSION()"))
            SnowflakeSchemaManager.reconcile_non_destructive(self.engine, self.settings.schema_name)
            return {"status": "ok"}
        return self.circuit_breaker_push.call(_do_startup)

    def shutdown(self):
        if self.engine:
            self.engine.dispose()

    def _upload_local_manifest(self, local_conn, cloud_conn, table_name: str, pk_col: str, hash_col: str) -> str:
        manifest_table = f"{table_name}_manifest_tmp"
        cloud_conn.execute(text(f"CREATE OR REPLACE TEMPORARY TABLE {self.settings.schema_name}.{manifest_table} (id VARCHAR, content_hash VARCHAR)"))
        try:
            cursor = local_conn.execute(f"SELECT {pk_col}, {hash_col} FROM {table_name}")
            manifest_rows = [{"id": str(row[0]), "content_hash": str(row[1])} for row in cursor.fetchall()]
        except Exception:
            manifest_rows = []
        if manifest_rows:
            batch_size = 10000
            for i in range(0, len(manifest_rows), batch_size):
                batch = manifest_rows[i:i + batch_size]
                cloud_conn.execute(text(f"INSERT INTO {self.settings.schema_name}.{manifest_table} (id, content_hash) VALUES (:id, :content_hash)"), batch)
        return manifest_table

    def _merge_to_cloud(self, cloud_conn, schema: str, table_name: str, columns: List[str], dict_rows: list, pk_col: str) -> int:
        stage_table = f"{table_name}_stage"
        col_defs = ",".join([f"{c} VARCHAR" for c in columns])
        cloud_conn.execute(text(f"CREATE OR REPLACE TEMPORARY TABLE {schema}.{stage_table} ({col_defs})"))
        insert_placeholders = ",".join([f":{c}" for c in columns])
        cloud_conn.execute(text(f"INSERT INTO {schema}.{stage_table} VALUES ({insert_placeholders})"), dict_rows)
        merge_sql = f"""
        MERGE INTO {schema}.{table_name} t
        USING {schema}.{stage_table} s
        ON t.{pk_col} = s.{pk_col}
        WHEN MATCHED THEN UPDATE SET {", ".join([f"t.{c} = s.{c}" for c in columns if c != pk_col])}
        WHEN NOT MATCHED THEN INSERT ({",".join(columns)}) VALUES ({",".join([f"s.{c}" for c in columns])})
        """
        result = cloud_conn.execute(text(merge_sql))
        return result.rowcount if hasattr(result, "rowcount") else len(dict_rows)

    def sync_data(self, local_db_path: str, tables: dict, batch_size: int = 500, delta_only: bool = True) -> dict:
        if not self.settings.enabled or not self.engine:
            return {"status": "disabled"}
        def _execute_cloud_sync():
            import sqlite3
            local_conn = None
            results = {}
            try:
                local_conn = sqlite3.connect(local_db_path, timeout=30.0)
                with self.engine.begin() as cloud_conn:
                    for table_name in tables:
                        if 'VIRTUAL' in tables[table_name].upper():
                            continue
                        pk_col = "id"
                        hash_col = "content_hash"
                        has_hash = False
                        try:
                            for col_info in local_conn.execute(f"PRAGMA table_info({table_name})").fetchall():
                                if col_info[5] > 0: pk_col = col_info[1]
                                if col_info[1] == "content_hash": has_hash = True
                            if not has_hash: hash_col = "''"
                        except Exception: pass
                        manifest_table = self._upload_local_manifest(local_conn, cloud_conn, table_name, pk_col, hash_col)
                        try:
                            diff_query = f"""
                            SELECT m.id FROM {self.settings.schema_name}.{manifest_table} m
                            LEFT JOIN {self.settings.schema_name}.{table_name} c ON m.id = c.{pk_col}
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
                            chunk = ids_to_push[i:i + chunk_size]
                            placeholders = ",".join("?" for _ in chunk)
                            cursor = local_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} IN ({placeholders})", chunk)
                            if not columns and cursor.description:
                                columns = [desc[0] for desc in cursor.description]
                            dict_rows.extend([dict(zip(columns, r)) for r in cursor.fetchall()])
                        if not dict_rows or not columns:
                            results[table_name] = 0
                            continue
                        if "embedding" in columns:
                            if self.vec0_settings.use_native_vector_type:
                                from database.backup.vec.cloud_vector_pusher import VectorSync
                                pusher = VectorSync(circuit_breaker=self.circuit_breaker_vec)
                                push_result = pusher.push_vectors(cloud_conn, self.settings.schema_name, table_name, columns, dict_rows, pk_col, batch_size=batch_size)
                                results[table_name] = push_result["pushed"]
                            else:
                                results[table_name] = self._merge_to_cloud(
                                    cloud_conn, self.settings.schema_name, table_name, columns, dict_rows, pk_col
                                )
                        else:
                            results[table_name] = self._merge_to_cloud(
                                cloud_conn, self.settings.schema_name, table_name, columns, dict_rows, pk_col
                            )
                return results
            finally:
                if local_conn is not None:
                    try:
                        local_conn.close()
                    except Exception:
                        pass
        return self.circuit_breaker_push.call(_execute_cloud_sync)

    def pull_to_local(self, local_db_path: str, tables: dict) -> dict:
        if not self.settings.enabled or not self.engine:
            return {"status": "disabled"}
        def _execute_pull():
            import datetime
            import struct
            from decimal import Decimal
            results = {}
            try:
                import sqlite3
                local_conn = sqlite3.connect(local_db_path, timeout=30.0)
                with self.engine.begin() as cloud_conn:
                    for table_name in tables:
                        if 'VIRTUAL' in tables[table_name].upper():
                            continue
                        pk_col = "id"
                        hash_col = "content_hash"
                        has_hash = False
                        try:
                            for col_info in local_conn.execute(f"PRAGMA table_info({table_name})").fetchall():
                                if col_info[5] > 0: pk_col = col_info[1]
                                if col_info[1] == "content_hash": has_hash = True
                            if not has_hash: hash_col = "''"
                        except Exception: pass
                        manifest_table = self._upload_local_manifest(local_conn, cloud_conn, table_name, pk_col, hash_col)
                        diff_query = f"""
                            SELECT c.* FROM {self.settings.schema_name}.{table_name} c
                            LEFT JOIN {self.settings.schema_name}.{manifest_table} m ON c.{pk_col} = m.id
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
                        if has_embedding and self.vec0_settings.use_native_vector_type:
                            from database.backup.vec.cloud_vector_pusher import VectorSync
                            vector_sync = VectorSync()
                            records, dlq_rows = vector_sync.pull_vectors_from_cloud(cloud_rows, columns)
                            if dlq_rows:
                                from database.writer import enqueue_write
                                import json
                                from utils.id_generator import ULID
                                for dlq_row in dlq_rows:
                                    safe_dlq_row = {k: v for k, v in dlq_row.items() if k != "_error_msg" and not isinstance(v, bytes)}
                                    enqueue_write(
                                        "INSERT INTO dead_letter_queue (dlq_id, table_name, row_id, row_data, error_message) VALUES (?, ?, ?, ?, ?)",
                                        (ULID.generate(), table_name, dlq_row.get(pk_col, ""), json.dumps(safe_dlq_row), dlq_row.get("_error_msg"))
                                    )
                        else:
                            records = []
                            for row in cloud_rows:
                                norm_row = []
                                for val in row:
                                    if isinstance(val, (datetime.datetime, datetime.date)):
                                        norm_row.append(val.isoformat())
                                    elif isinstance(val, Decimal):
                                        norm_row.append(float(val))
                                    elif isinstance(val, list) and len(val) > 0 and isinstance(val[0], float):
                                        norm_row.append(struct.pack(f'<{len(val)}f', *val))
                                    else:
                                        norm_row.append(val)
                                records.append(dict(zip(columns, norm_row)))
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
                            log.dual_log(
                                tag="Backup:Pull:Written",
                                message=f"Wrote {len(records)} rows to local table {table_name}",
                                level="INFO",
                                payload={"table": table_name, "rows": len(records)}
                            )
                        results[table_name] = len(records)
            finally:
                local_conn.close()
            return results
        return self.circuit_breaker_pull.call(_execute_pull)
