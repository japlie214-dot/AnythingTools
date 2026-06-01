# database/backup/engine/dual_engine.py
from database.backup.settings import BackupSettings
from database.backup.engine.local_engine import LocalEngine
from database.backup.engine.cloud_engine import CloudEngine
from database.backup.writer.backup_writer import enqueue_backup_write
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class DualEngine:
    def __init__(self, settings: BackupSettings):
        self.settings = settings
        self.local = LocalEngine(settings.local)
        self.cloud = CloudEngine(settings.cloud, settings.sync)

    def startup(self) -> dict:
        results = {}
        try:
            results["local"] = self.local.startup()
            self._ensure_fallback_table()
        except Exception as e:
            results["local"] = {"status": "error", "error": str(e)}
            log.dual_log(tag="Backup:Dual:Startup", message="Local engine startup failed", level="CRITICAL", payload={"error": str(e)})
        try:
            results["cloud"] = self.cloud.startup()
        except Exception as e:
            results["cloud"] = {"status": "error", "error": str(e)}
            log.dual_log(tag="Backup:Dual:Startup", message="Cloud engine startup failed, operating in degraded mode", level="WARNING", payload={"error": str(e)})
        return results

    def _ensure_fallback_table(self):
        enqueue_backup_write("""
            CREATE TABLE IF NOT EXISTS sync_fallback_queue (
                table_name TEXT NOT NULL,
                row_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('UPSERT', 'DELETE')),
                queued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (table_name, row_id)
            );
        """)

    def shutdown(self):
        try:
            self.cloud.shutdown()
        except Exception:
            pass
        try:
            self.local.shutdown()
        except Exception:
            pass
