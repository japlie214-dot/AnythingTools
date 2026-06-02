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
        except Exception as e:
            results["local"] = {"status": "error", "error": str(e)}
            log.dual_log(tag="Backup:Dual:Startup", message="Local engine startup failed", level="CRITICAL", payload={"error": str(e)})
        try:
            results["cloud"] = self.cloud.startup()
        except Exception as e:
            results["cloud"] = {"status": "error", "error": str(e)}
            log.dual_log(tag="Backup:Dual:Startup", message="Cloud engine startup failed, operating in degraded mode", level="WARNING", payload={"error": str(e)})
        return results

    def shutdown(self):
        try:
            self.cloud.shutdown()
        except Exception:
            pass
        try:
            self.local.shutdown()
        except Exception:
            pass

    def sync_all(self, mode: str = "delta") -> dict:
        import time
        from database.backup.schema_registry import BackupSchemaRegistry
        
        start_time = time.time()
        tables = BackupSchemaRegistry.get_expected_sqlite_tables()
        results = {"local": {}, "cloud": {}, "duration": 0.0}

        try:
            results["local"] = self.local.sync_data(tables, mode=mode)
            # Wait for local writes to finish before cloud sync
            from database.backup.writer.backup_writer import backup_write_queue
            import time
            start_wait = time.monotonic()
            while backup_write_queue.unfinished_tasks > 0:
                if time.monotonic() - start_wait > 60.0:
                    break
                time.sleep(0.1)
        except Exception as e:
            log.dual_log(tag="Backup:Sync:LocalError", message=f"Local sync failed: {str(e)}", level="ERROR", payload={"error": str(e)})
            results["local_error"] = str(e)
            return results

        if self.cloud.settings.enabled:
            try:
                from database.backup.resilience.circuit_breaker import CircuitOpenError
                results["cloud"] = self.cloud.sync_data(self.local.db_path, tables, batch_size=self.settings.sync.batch_size)
            except Exception as e:
                log.dual_log(tag="Backup:Sync:CloudError", message=f"Cloud sync failed: {str(e)}", level="ERROR", payload={"error": str(e)})
                results["cloud_error"] = str(e)

        results["duration"] = time.time() - start_time
        return results

    def restore_pipeline(self) -> bool:
        return True # Implement actual restore push-down as needed

    def sync_all(self, mode: str = "delta") -> dict:
        import time
        from database.backup.schema_registry import BackupSchemaRegistry
        
        start_time = time.time()
        tables = BackupSchemaRegistry.get_expected_sqlite_tables()
        results = {"local": {}, "cloud": {}, "duration": 0.0}

        try:
            results["local"] = self.local.sync_data(tables, mode=mode)
        except Exception as e:
            log.dual_log(tag="Backup:Sync:LocalError", message=f"Local sync failed: {str(e)}", level="ERROR", payload={"error": str(e)})
            results["local_error"] = str(e)
            return results

        if self.cloud.settings.enabled:
            try:
                from database.backup.resilience.circuit_breaker import CircuitOpenError
                results["cloud"] = self.cloud.sync_data(self.local.db_path, tables, batch_size=self.settings.sync.batch_size)
            except Exception as e:
                log.dual_log(tag="Backup:Sync:CloudError", message=f"Cloud sync failed: {str(e)}", level="ERROR", payload={"error": str(e)})
                results["cloud_error"] = str(e)

        results["duration"] = time.time() - start_time
        return results

    def restore_pipeline(self) -> bool:
        return True # Implement actual restore push-down as needed
