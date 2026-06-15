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


from database.backup.engine.base import BackupEngine
from database.backup.engine.schema_manager import SnowflakeSchemaManager

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
            SnowflakeSchemaManager.reconcile(self.engine, self.settings.schema_name)
            
            # Reconcile types and rebuild if mismatched
            mismatch_plans = SnowflakeSchemaManager.reconcile_types(self.engine, self.settings.schema_name)
            for plan in mismatch_plans:
                from database.connection import DB_PATH
                SnowflakeSchemaManager.rebuild_table(self.engine, self.settings.schema_name, plan, str(DB_PATH))
                
            return {"status": "ok"}
        return self.circuit_breaker_push.call(_do_startup)

    def shutdown(self):
        if self.engine:
            self.engine.dispose()

    def sync_data(self, local_db_path: str, tables: dict, batch_size: int = 500, delta_only: bool = True) -> dict:
        from database.backup.engine.sync_operations import sync_data as _sync_data
        return _sync_data(self, local_db_path, tables, batch_size, delta_only)

    def pull_to_local(self, local_db_path: str, tables: dict) -> dict:
        from database.backup.engine.sync_operations import pull_to_local as _pull
        return _pull(self, local_db_path, tables)
