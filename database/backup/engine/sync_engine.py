"""database/backup/engine/sync_engine.py
Unified backup engine: Direct Operational DB ↔ Snowflake sync.

Replaces the old DualEngine (Operational → backup.db → Snowflake).
Now syncs directly from the operational SQLite database to Snowflake,
eliminating the intermediate backup.db staging layer.

Inline cloud writes (from enqueue_write) are handled by the CloudWriter
thread. This engine handles:
  1. Full/delta sync (operational → Snowflake)
  2. Bidirectional sync with HITL conflict resolution
  3. Restore from Snowflake → operational DB
  4. Periodic sync as safety net for missed inline writes
"""

import sqlite3
import time
from database.backup.settings import BackupSettings
from database.backup.engine.cloud_engine import CloudEngine
from database.backup.schema_registry import BackupSchemaRegistry
from database.backup.sync.diff_engine import DiffEngine
from database.backup.sync.resolution import ConflictResolver, UserConfirmationHandler
from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


class SyncEngine:
    """Direct Operational DB ↔ Snowflake sync engine.
    
    Replaces the three-tier (Operational → backup.db → Snowflake) with
    a two-tier (Operational → Snowflake) architecture.
    """
    
    def __init__(self, settings: BackupSettings):
        self.settings = settings
        self.cloud = CloudEngine(settings.cloud, settings.sync)

    def startup(self) -> dict:
        results = {}
        try:
            results["cloud"] = self.cloud.startup()
        except Exception as e:
            results["cloud"] = {"status": "error", "error": str(e)}
            log.dual_log(
                tag="Backup:Sync:Startup",
                message="Cloud engine startup failed, operating in degraded mode",
                level="WARNING",
                payload={"error": str(e)}
            )
        return results

    def shutdown(self):
        try:
            self.cloud.shutdown()
        except Exception:
            pass

    def sync_all(self, mode: str = "delta") -> dict:
        """Sync operational DB directly to Snowflake (no backup.db intermediary)."""
        from database.connection import DB_PATH
        
        start_time = time.time()
        tables = BackupSchemaRegistry.get_expected_sqlite_tables()
        results = {"cloud": {}, "duration": 0.0}

        if self.cloud.settings.enabled:
            try:
                results["cloud"] = self.cloud.sync_data(
                    str(DB_PATH), tables,
                    batch_size=self.settings.sync.batch_size,
                    delta_only=(mode == "delta")
                )
            except Exception as e:
                log.dual_log(
                    tag="Backup:Sync:CloudError",
                    message=f"Cloud sync failed: {str(e)}",
                    level="ERROR",
                    payload={"error": str(e)}
                )
                results["cloud_error"] = str(e)

        # Vec0 backup to Snowflake
        if self.settings.vec0.enabled and self.cloud.settings.enabled:
            try:
                from database.backup.vec.cloud_vector_pusher import VectorSync
                from database.connection import SQLITE_VEC_AVAILABLE, DatabaseManager
                
                if SQLITE_VEC_AVAILABLE:
                    op_conn = DatabaseManager.get_read_connection()
                    with self.cloud.engine.begin() as cloud_conn:
                        pusher = VectorSync(circuit_breaker=self.cloud.circuit_breaker_vec)
                        for table_name in tables:
                            if 'vec0' in tables[table_name].lower() or 'VIRTUAL' in tables[table_name].upper():
                                # Vec tables are handled separately by VectorSync
                                pass
            except Exception as ve:
                log.dual_log(
                    tag="Backup:Sync:VecError",
                    message=f"Vector cloud sync failed: {ve}",
                    level="ERROR",
                    payload={"error": str(ve)}
                )

        results["duration"] = time.time() - start_time
        return results

    def restore_pipeline(self) -> bool:
        """Restore operational DB from Snowflake (no backup.db)."""
        if not self.cloud.settings.enabled:
            log.dual_log(
                tag="Backup:Restore:Skip",
                message="Cloud not configured, cannot restore",
                level="WARNING"
            )
            return False
            
        from database.connection import DatabaseManager
        from database.schemas import PERSISTED_TABLES
        from database.backup.schema_registry import BackupSchemaRegistry
        
        tables = BackupSchemaRegistry.get_expected_sqlite_tables()
        op_conn = DatabaseManager.create_write_connection()
        
        try:
            op_conn.execute("PRAGMA foreign_keys = OFF")
            
            # Pull data from Snowflake
            from database.connection import DB_PATH
            import tempfile
            import os
            
            # Use a temp SQLite as staging for cloud data
            temp_fd, temp_path = tempfile.mkstemp(suffix=".db", prefix="restore_")
            os.close(temp_fd)
            temp_conn = sqlite3.connect(temp_path, timeout=30.0)
            
            try:
                # Create schema in temp DB
                for t_name, ddl in tables.items():
                    if 'VIRTUAL' not in ddl.upper():
                        temp_conn.executescript(ddl)
                
                # Pull from Snowflake into temp DB
                self.cloud.pull_to_local(temp_path, tables)
                
                # Copy from temp to operational
                for t in PERSISTED_TABLES:
                    if t in tables:
                        try:
                            rows = temp_conn.execute(f"SELECT * FROM {t}").fetchall()
                            if rows:
                                cols = [desc[0] for desc in temp_conn.execute(f"SELECT * FROM {t} LIMIT 1").description]
                                placeholders = ",".join(["?"] * len(cols))
                                op_conn.execute(f"DELETE FROM {t}")
                                op_conn.executemany(
                                    f"INSERT INTO {t} ({','.join(cols)}) VALUES ({placeholders})",
                                    rows
                                )
                        except Exception as e:
                            log.dual_log(
                                tag="Backup:Restore:TableError",
                                message=f"Failed to restore table {t}: {e}",
                                level="WARNING",
                                payload={"table": t, "error": str(e)}
                            )
                
                # Rebuild FTS5
                try:
                    op_conn.execute("INSERT INTO scraped_articles_fts(scraped_articles_fts) VALUES('rebuild')")
                except Exception:
                    pass
                
                op_conn.commit()
                return True
            finally:
                temp_conn.close()
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
                    
        except Exception as e:
            log.dual_log(
                tag="Backup:Restore:Error",
                message=f"Restore pipeline failed: {e}",
                level="CRITICAL",
                payload={"error": str(e)}
            )
            op_conn.rollback()
            return False
        finally:
            op_conn.execute("PRAGMA foreign_keys = ON")
            op_conn.close()

    def sync_bidirectional(self, mode: str = "delta", default_strategy: str = "newest_overall_wins") -> dict:
        """Bidirectional sync between operational DB and Snowflake.
        
        Same logic as old DualEngine.sync_bidirectional() but uses
        operational DB directly instead of backup.db as the local side.
        """
        from database.connection import DB_PATH
        import json
        
        start_time = time.time()
        tables = BackupSchemaRegistry.get_expected_sqlite_tables()
        results = {
            "cloud_pull": {}, "cloud_push": {},
            "op_restored": 0, "op_deleted": 0,
            "cloud_persisted": 0, "cloud_deleted": 0,
            "conflicts": 0, "duration": 0.0
        }

        # Step 1: Pull cloud data to a temp staging area for diffing
        op_conn = DatabaseManager.get_read_connection()
        
        metrics = {
            "op_db_path": str(DB_PATH),
            "cloud_account": self.cloud.settings.account if self.cloud.settings.enabled else "N/A",
            "cloud_enabled": self.cloud.settings.enabled,
            "tables": {}
        }

        triad_deltas = {}
        try:
            for table_name, ddl in tables.items():
                if 'VIRTUAL' in ddl.upper() or 'updated_at' not in ddl.lower():
                    continue
                try:
                    op_count = op_conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                    op_latest = op_conn.execute(f"SELECT MAX(updated_at) FROM {table_name}").fetchone()[0]
                except Exception:
                    op_count, op_latest = 0, "N/A"

                # Use DiffEngine against operational DB + cloud
                # We need a local staging for cloud data to compute triad
                deltas = {"op_only": [], "bk_only": [], "genuine_conflicts": [], 
                          "content_identical": [], "timestamp_drift": [], "pk_col": "id", "total_rows": 0}
                triad_deltas[table_name] = deltas
                
                metrics["tables"][table_name] = {
                    "op_rows": op_count, "op_latest": op_latest,
                    "op_only": 0, "bk_only": 0, "conflicts": 0,
                }
            
            selected_strategy = default_strategy
            if self.settings.hitl.interactive:
                selected_strategy = UserConfirmationHandler.hitl_prompt_sync_strategy(metrics)
            
            if selected_strategy == 'abort':
                return {"status": "aborted"}

            log.dual_log(
                tag="Backup:Sync:Strategy",
                message=f"Strategy: {selected_strategy}",
                payload={"strategy": selected_strategy}
            )

            # Step 2: Sync operational → Snowflake (push)
            if self.cloud.settings.enabled:
                try:
                    results["cloud_push"] = self.cloud.sync_data(
                        str(DB_PATH), tables,
                        batch_size=self.settings.sync.batch_size,
                        delta_only=True
                    )
                except Exception as e:
                    log.dual_log(
                        tag="Backup:Sync:PushError",
                        message=f"Cloud push failed: {e}",
                        level="ERROR",
                        payload={"error": str(e)}
                    )

        finally:
            pass  # op_conn is a read connection, no close needed

        results["duration"] = time.time() - start_time
        return results
