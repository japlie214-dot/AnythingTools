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

            # Ensure the vector extension is loaded on the backup connection if available
            from database.connection import _attempt_vec_load
            _attempt_vec_load(conn)

            expected_tables = BackupSchemaRegistry.get_expected_sqlite_tables()
            from database.backup.sync.foundation import SyncLedger
            from database.schemas.sync_audit import TABLES as AUDIT_TABLES
            
            # Register infrastructure tables so the Reconciler manages and protects them
            expected_tables.update(AUDIT_TABLES)
            expected_tables["sync_ledger"] = SyncLedger.SCHEMA
            expected_tables["dead_letter_queue"] = """
                CREATE TABLE IF NOT EXISTS dead_letter_queue (
                    table_name TEXT NOT NULL,
                    row_id TEXT NOT NULL,
                    row_data TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (table_name, row_id)
                );
            """

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


    def sync_data(self, tables: dict, mode: str = "delta") -> dict:
        from database.connection import DatabaseManager
        from database.backup.writer.backup_writer import enqueue_backup_write
        from database.backup.sync.foundation import SyncLedger
        
        op_conn = DatabaseManager.get_read_connection()
        results = {}

        for table_name, ddl in tables.items():
            if 'VIRTUAL' in ddl.upper():
                continue

            ts_col = "updated_at" if "updated_at" in ddl.lower() else None
            
            if mode == "delta" and ts_col:
                watermark = self._get_table_watermark(table_name)
                cursor = op_conn.execute(f"SELECT * FROM {table_name} WHERE {ts_col} > ?", (watermark,))
            else:
                cursor = op_conn.execute(f"SELECT * FROM {table_name}")

            rows = cursor.fetchall()
            if not rows:
                results[table_name] = 0
                continue

            columns = [desc[0] for desc in cursor.description]
            placeholders = ",".join(["?"] * len(columns))
            column_names = ",".join(columns)
            
            batch_size = 1000
            from database.backup.writer.backup_writer import BackupWriteTask
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                records = [dict(zip(columns, r)) for r in batch]
                pk_col = "id"  # Implicit default standard
                enqueue_backup_write(BackupWriteTask(table_name, "UPSERT", records, pk_col))
            results[table_name] = len(rows)

        if sum(results.values()) > 0:
            from database.backup.writer.backup_writer import BackupWriteTask
            from database.backup.sync.foundation import SyncLedger
            ledger_records = [{
                "operation_id": SyncLedger.now_iso(),
                "table_name": "ALL",
                "direction": "BIDIRECTIONAL",
                "row_count": sum(results.values()),
                "state": "COMPLETED",
                "completed_at": SyncLedger.now_iso()
            }]
            enqueue_backup_write(BackupWriteTask("sync_ledger", "UPSERT", ledger_records, "operation_id"))
        return results

    def _get_table_watermark(self, table_name: str) -> str:
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute("SELECT max(completed_at) FROM sync_ledger WHERE state = 'COMPLETED'")
            val = cursor.fetchone()[0]
            return val if val else "1970-01-01T00:00:00"
        except sqlite3.OperationalError:
            return "1970-01-01T00:00:00"
        finally:
            conn.close()
