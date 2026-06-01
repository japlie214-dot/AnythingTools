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
            res = conn.execute(text(f"""
                SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = '{schema_name.upper()}'
            """))
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

class CloudEngine:
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
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=cb_settings.circuit_breaker_threshold,
            reset_timeout=cb_settings.circuit_breaker_reset_seconds
        )

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
            SnowflakeSchemaManager.reconcile_non_destructive(self.engine, self.settings.schema_name)
            return {"status": "ok"}
        return self.circuit_breaker.call(_do_startup)

    def shutdown(self):
        if self.engine:
            self.engine.dispose()
