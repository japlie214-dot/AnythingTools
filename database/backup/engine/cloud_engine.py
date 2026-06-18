# database/backup/engine/cloud_engine.py
import warnings
import struct
from typing import List
from sqlalchemy import create_engine, text
from database.backup.settings import CloudBackupSettings
from database.backup.schema_registry import BackupSchemaRegistry
from database.backup.resilience.circuit_breaker import CircuitBreaker
from database.backup.sync.helpers import introspect_table_columns, normalize_cloud_row
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

# Suppress the RequestsDependencyWarning emitted by snowflake-connector-python's
# vendored requests library when chardet 7.x is installed transitively.
# This is a known Snowflake-side bug (issue #2883, open as of v4.6.0):
# https://github.com/snowflakedb/snowflake-connector-python/issues/2883
# The warning is cosmetic (the connector's actual dependencies are satisfied
# by charset_normalizer) and the suppression is wrapped in try/except so it
# cannot break startup if the import path changes in a future connector release.
try:
    from snowflake.connector.vendored.requests.exceptions import RequestsDependencyWarning
    warnings.simplefilter("ignore", RequestsDependencyWarning)
except Exception:
    pass


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
                connect_args={
                    'private_key': self._load_private_key(),
                    # Keep the Snowflake session alive indefinitely. Per the
                    # Snowflake docs, the connector emits a periodic heartbeat
                    # that refreshes the master token, preventing 390111 errors
                    # caused by idle-session timeout.
                    # https://docs.snowflake.com/en/sql-reference/parameters
                    # (CLIENT_SESSION_KEEP_ALIVE — "Snowflake keeps the session
                    # active indefinitely as long as the connection is active")
                    'client_session_keep_alive': True,
                    # Heartbeat frequency: 1800 s (30 min) — within the allowed
                    # range [900, 3600] per Snowflake docs
                    # (CLIENT_SESSION_KEEP_ALIVE_HEARTBEAT_FREQUENCY).
                    'client_session_keep_alive_heartbeat_frequency': 1800,
                    # Login and network retry behavior per Snowflake Python
                    # Connector docs:
                    # https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-connect
                    # (Section: "Managing connection timeouts")
                    'login_timeout': 60,
                    'network_timeout': 30,
                },
                pool_size=settings.pool_size,
                max_overflow=settings.max_overflow,
                # pool_pre_ping runs a liveness check at checkout time only.
                # Per SQLAlchemy docs: "the pre-ping approach does not
                # accommodate for connections dropped in the middle of
                # transactions or other SQL operations."
                # https://docs.sqlalchemy.org/en/20/core/pooling.html#sqlalchemy.create_engine.params.pool_pre_ping
                # We keep it ON as a first line of defense; the handle_error
                # listener + with_session_recovery decorator (registered
                # below) handle mid-statement 390111 errors that pre-ping
                # cannot catch.
                pool_pre_ping=True,
                # Recycle connections proactively. Even with client_session_keep_alive,
                # Snowflake may invalidate sessions server-side (admin kill,
                # account policy change, network partition). pool_recycle
                # ensures we never hold a connection longer than 1 hour.
                # https://docs.sqlalchemy.org/en/20/core/pooling.html#sqlalchemy.engine.Engine.dispose
                pool_recycle=3600,
            )

            # Register the handle_error listener that recognises Snowflake
            # 390111 as a disconnect condition, forcing pool invalidation.
            # This complements pool_pre_ping (which only checks at checkout)
            # by also handling mid-statement session-gone errors.
            # https://docs.sqlalchemy.org/en/20/core/events.html#sqlalchemy.events.DialectEvents.handle_error
            from database.backup.resilience.session_recovery import register_session_recovery
            register_session_recovery(self.engine, log)
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
