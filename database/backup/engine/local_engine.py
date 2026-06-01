# database/backup/engine/local_engine.py
import sqlite3
from database.backup.engine.base import BackupEngine
from database.backup.settings import LocalBackupSettings
from database.backup.writer.backup_writer import start_backup_writer
from database.management.reconciler import SchemaReconciler
from database.backup.schema_registry import BackupSchemaRegistry
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class LocalEngine(BackupEngine):
    def __init__(self, settings: LocalBackupSettings):
        self.settings = settings
        self.db_path = settings.db_path

    def startup(self) -> dict:
        if not self.settings.enabled:
            return {"status": "disabled"}

        start_backup_writer(self.db_path)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            res = conn.execute("PRAGMA integrity_check").fetchone()
            if res[0] != "ok":
                raise RuntimeError(f"Backup DB corruption detected: {res[0]}")

            expected_tables = BackupSchemaRegistry.get_expected_sqlite_tables()
            from database.backup.sync.ledger import SyncLedger
            conn.executescript(SyncLedger.SCHEMA)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS dead_letter_queue (
                    dlq_id TEXT PRIMARY KEY,
                    table_name TEXT NOT NULL,
                    row_id TEXT NOT NULL,
                    row_data TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)

            class NonDestructiveReconciler(SchemaReconciler):
                def _prune_unexpected(self, report):
                    pass

            ReconcilerClass = SchemaReconciler if self.settings.allow_drop_tables else NonDestructiveReconciler
            
            reconciler = ReconcilerClass(
                conn=conn,
                label="BackupDB",
                expected_tables=expected_tables,
                expected_triggers={},
                master_tables=list(expected_tables.keys())
            )
            report = reconciler.reconcile()
            return {"status": "ok", "actions": [a.action for a in report.actions]}
        finally:
            conn.close()

    def shutdown(self) -> None:
        from database.backup.writer.backup_writer import _backup_shutdown, backup_write_queue
        _backup_shutdown.set()
        try:
            backup_write_queue.put(None, timeout=2.0)
        except Exception:
            pass
