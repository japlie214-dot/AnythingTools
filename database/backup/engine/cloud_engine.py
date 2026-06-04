# database/backup/engine/cloud_engine.py
import struct
from typing import List
from sqlalchemy import create_engine, text
from database.backup.settings import CloudBackupSettings
from database.backup.schema_registry import BackupSchemaRegistry
from database.backup.resilience.circuit_breaker import CircuitBreaker
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

def unpack_vector(blob: bytes) -> List[float]:
    if not blob or len(blob) != 4096:
        return []
    return list(struct.unpack('<1024f', blob))

from database.backup.engine.base import BackupEngine

class CloudEngine(BackupEngine):
    def __init__(self, settings: CloudBackupSettings, cb_settings):
        self.settings = settings
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
        
        # Isolate circuit breakers into discrete instances
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
        self.circuit_breaker = self.circuit_breaker_push # Backward compatibility

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

    def sync_data(self, local_db_path: str, tables: dict, batch_size: int = 500) -> dict:
        if not self.settings.enabled or not self.engine:
            return {"status": "disabled"}

        def _execute_cloud_sync():
            import sqlite3
            local_conn = sqlite3.connect(local_db_path, timeout=30.0)
            results = {}
            
            try:
                with self.engine.begin() as cloud_conn:
                    for table_name in tables:
                        if 'VIRTUAL' in tables[table_name].upper():
                            continue
                        
                        cursor = local_conn.execute(f"SELECT * FROM {table_name}")
                        rows = cursor.fetchall()
                        if not rows:
                            results[table_name] = 0
                            continue

                        columns = [desc[0] for desc in cursor.description]
                        placeholders = ",".join([f":{c}" for c in columns])
                        dict_rows = [dict(zip(columns, r)) for r in rows]

                        try:
                            stage_table = f"{table_name}_stage"
                            col_defs = ",".join([f"{c} VARCHAR" for c in columns])
                            cloud_conn.execute(text(f"CREATE OR REPLACE TEMPORARY TABLE {self.settings.schema_name}.{stage_table} ({col_defs})"))
                            
                            cloud_conn.execute(
                                text(f"INSERT INTO {self.settings.schema_name}.{stage_table} VALUES ({placeholders})"),
                                dict_rows
                            )
                            
                            pk_col = "id"
                            try:
                                for col_info in local_conn.execute(f"PRAGMA table_info({table_name})").fetchall():
                                    if col_info[5] > 0:  # The 5th index represents the PK flag
                                        pk_col = col_info[1]
                                        break
                            except Exception:
                                pass

                            merge_sql = f"""
                            MERGE INTO {self.settings.schema_name}.{table_name} t
                            USING {self.settings.schema_name}.{stage_table} s
                            ON t.{pk_col} = s.{pk_col}
                            WHEN MATCHED THEN UPDATE SET
                                {", ".join([f"t.{c} = s.{c}" for c in columns if c != pk_col])}
                            WHEN NOT MATCHED THEN INSERT ({",".join(columns)})
                                VALUES ({",".join([f"s.{c}" for c in columns])})
                            """
                            result = cloud_conn.execute(text(merge_sql))
                            results[table_name] = result.rowcount if hasattr(result, "rowcount") else len(dict_rows)
                        except Exception as inner_e:
                            log.dual_log(tag="Backup:Cloud:PushTableError", message=f"Failed pushing table {table_name}", level="ERROR", payload={"error": str(inner_e)})
                            raise
                return results
            finally:
                local_conn.close()

        try:
            return self.circuit_breaker_push.call(_execute_cloud_sync)
        except Exception as e:
            raise e

    def pull_to_local(self, local_db_path: str, tables: dict) -> dict:
        if not self.settings.enabled or not self.engine:
            return {"status": "disabled"}
            
        import sqlite3
        import datetime
        from decimal import Decimal
        local_conn = sqlite3.connect(local_db_path, timeout=30.0)
        results = {}
        
        try:
            with self.engine.begin() as cloud_conn:
                for table_name in tables:
                    if 'VIRTUAL' in tables[table_name].upper():
                        continue
                        
                    cloud_rows = cloud_conn.execute(
                        text(f"SELECT * FROM {self.settings.schema_name}.{table_name}")
                    ).fetchall()
                    
                    if not cloud_rows:
                        results[table_name] = 0
                        continue
                        
                    columns = [k.lower() for k in cloud_rows[0]._mapping.keys()]
                    col_names = ",".join(columns)
                    placeholders = ",".join(["?"] * len(columns))
                    
                    normalized_rows = []
                    for row in cloud_rows:
                        norm_row = []
                        for val in row:
                            if isinstance(val, (datetime.datetime, datetime.date)):
                                norm_row.append(val.isoformat())
                            elif isinstance(val, Decimal):
                                norm_row.append(float(val))
                            else:
                                norm_row.append(val)
                        normalized_rows.append(tuple(norm_row))
                        
                    local_conn.executemany(
                        f"INSERT OR REPLACE INTO {table_name} ({col_names}) VALUES ({placeholders})",
                        normalized_rows
                    )
                    results[table_name] = len(normalized_rows)
            local_conn.commit()
        finally:
            local_conn.close()
        return results
