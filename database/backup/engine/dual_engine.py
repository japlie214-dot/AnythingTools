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
                results["cloud"] = self.cloud.sync_data(self.local.db_path, tables, batch_size=self.settings.sync.batch_size)
            except Exception as e:
                log.dual_log(tag="Backup:Sync:CloudError", message=f"Cloud sync failed: {str(e)}", level="ERROR", payload={"error": str(e)})
                results["cloud_error"] = str(e)

        results["duration"] = time.time() - start_time
        return results

    def restore_pipeline(self) -> bool:
        return True # Implement actual restore push-down as needed

    def sync_bidirectional(self, mode: str = "delta", default_strategy: str = "newest_overall_wins") -> dict:
        import time
        import sqlite3
        from database.backup.schema_registry import BackupSchemaRegistry
        from database.backup.sync.diff_engine import DiffEngine
        from database.backup.sync.conflict_resolver import ConflictResolver
        from database.backup.sync.user_confirmation import UserConfirmationHandler
        from database.connection import DatabaseManager
        from database.writer import enqueue_transaction
        from database.backup.writer.backup_writer import enqueue_backup_write, backup_write_queue
        
        start_time = time.time()
        tables = BackupSchemaRegistry.get_expected_sqlite_tables()
        results = {"cloud_pull": {}, "cloud_push": {}, "op_restored": 0, "op_deleted": 0, "bk_persisted": 0, "bk_deleted": 0, "conflicts": 0, "duration": 0.0}

        log.dual_log(
            tag="Backup:Sync:Config", 
            message="Bidirectional Sync Configurations", 
            payload={
                "local_db": self.local.db_path,
                "local_enabled": self.local.settings.enabled,
                "cloud_account": self.cloud.settings.account if self.cloud.settings.enabled else "N/A",
                "cloud_enabled": self.cloud.settings.enabled
            }
        )

        if self.cloud.settings.enabled:
            log.dual_log(tag="Backup:Sync:CloudPullRequest", message="Pulling cloud data to local backup", payload={"tables": list(tables.keys())})
            results["cloud_pull"] = self.cloud.pull_to_local(self.local.db_path, tables)
            log.dual_log(tag="Backup:Sync:CloudPullResponse", message="Cloud pull complete", payload={"results": results["cloud_pull"]})

        op_conn = DatabaseManager.get_read_connection()
        bk_conn = sqlite3.connect(self.local.db_path, timeout=30.0)
        
        metrics = {
            "op_db_path": "sumanal.db",
            "bk_db_path": self.local.db_path,
            "local_enabled": self.local.settings.enabled,
            "cloud_account": self.cloud.settings.account if self.cloud.settings.enabled else "N/A",
            "cloud_enabled": self.cloud.settings.enabled,
            "tables": {}
        }

        triad_deltas = {}
        try:
            # GATHER METRICS
            for table_name, ddl in tables.items():
                if 'VIRTUAL' in ddl.upper() or 'updated_at' not in ddl.lower():
                    continue
                try:
                    op_count = op_conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                    op_latest = op_conn.execute(f"SELECT MAX(updated_at) FROM {table_name}").fetchone()[0]
                except Exception: op_count, op_latest = 0, "N/A"
                try:
                    bk_count = bk_conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                    bk_latest = bk_conn.execute(f"SELECT MAX(updated_at) FROM {table_name}").fetchone()[0]
                except Exception: bk_count, bk_latest = 0, "N/A"

                deltas = DiffEngine.compute_triad_deltas(op_conn, bk_conn, table_name)
                triad_deltas[table_name] = deltas
                
                metrics["tables"][table_name] = {
                    "op_rows": op_count, "bk_rows": bk_count, "op_latest": op_latest, "bk_latest": bk_latest,
                    "op_only": len(deltas["op_only"]), "bk_only": len(deltas["bk_only"]), "conflicts": len(deltas["conflicts"])
                }
            
            # HITL CONFIRMATION
            selected_strategy = UserConfirmationHandler.hitl_prompt_sync_strategy(metrics)
            if selected_strategy == 'abort':
                log.dual_log(tag="Backup:Sync:Abort", message="Sync aborted via HITL", level="WARNING", payload={"metrics": metrics})
                return {"status": "aborted"}

            log.dual_log(tag="Backup:Sync:Strategy", message=f"Strategy selected: {selected_strategy}", payload={"strategy": selected_strategy, "metrics": metrics})

            # EXECUTE SYNC
            op_transactions = []
            for table_name, deltas in triad_deltas.items():
                pk_col = deltas["pk_col"]
                
                if selected_strategy == "operational_wins":
                    for bk_id in deltas["bk_only"]:
                        enqueue_backup_write(f"DELETE FROM {table_name} WHERE {pk_col} = ?", (bk_id,))
                        results["bk_deleted"] += 1
                        log.dual_log(tag="Backup:Sync:Verdict", message="Deleted row from backup", payload={"table": table_name, "id": bk_id, "action": "DELETE_FROM_BK", "reason": "operational_wins"})
                    for op_id in deltas["op_only"]:
                        row = op_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} = ?", (op_id,)).fetchone()
                        cols = [desc[1] for desc in op_conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
                        enqueue_backup_write(f"INSERT OR REPLACE INTO {table_name} ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})", tuple(row))
                        results["bk_persisted"] += 1
                        log.dual_log(tag="Backup:Sync:Verdict", message="Persisted row to backup", payload={"table": table_name, "id": op_id, "action": "PERSIST_TO_BK", "reason": "operational_wins"})

                elif selected_strategy == "backup_wins":
                    for op_id in deltas["op_only"]:
                        op_transactions.append((f"DELETE FROM {table_name} WHERE {pk_col} = ?", (op_id,)))
                        results["op_deleted"] += 1
                        log.dual_log(tag="Backup:Sync:Verdict", message="Deleted row from operational", payload={"table": table_name, "id": op_id, "action": "DELETE_FROM_OP", "reason": "backup_wins"})
                    for bk_id in deltas["bk_only"]:
                        row = bk_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} = ?", (bk_id,)).fetchone()
                        cols = [desc[1] for desc in bk_conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
                        op_transactions.append((f"INSERT OR REPLACE INTO {table_name} ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})", tuple(row)))
                        results["op_restored"] += 1
                        log.dual_log(tag="Backup:Sync:Verdict", message="Restored row to operational", payload={"table": table_name, "id": bk_id, "action": "RESTORE_TO_OP", "reason": "backup_wins"})

                else: # newest_overall_wins
                    for bk_id in deltas["bk_only"]:
                        row = bk_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} = ?", (bk_id,)).fetchone()
                        cols = [desc[1] for desc in bk_conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
                        op_transactions.append((f"INSERT OR REPLACE INTO {table_name} ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})", tuple(row)))
                        results["op_restored"] += 1
                        log.dual_log(tag="Backup:Sync:Verdict", message="Restored row to operational", payload={"table": table_name, "id": bk_id, "action": "RESTORE_TO_OP", "reason": "merge_bk_only"})
                    for op_id in deltas["op_only"]:
                        row = op_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} = ?", (op_id,)).fetchone()
                        cols = [desc[1] for desc in op_conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
                        enqueue_backup_write(f"INSERT OR REPLACE INTO {table_name} ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})", tuple(row))
                        results["bk_persisted"] += 1
                        log.dual_log(tag="Backup:Sync:Verdict", message="Persisted row to backup", payload={"table": table_name, "id": op_id, "action": "PERSIST_TO_BK", "reason": "merge_op_only"})

                for conflict in deltas["conflicts"]:
                    verdict = ConflictResolver.resolve_triad(conflict, strategy=selected_strategy)
                    if verdict == "manual":
                        verdict = UserConfirmationHandler.hitl_wait_for_sync_operator(
                            table_name, conflict["id"], conflict["operational_ts"], conflict["backup_ts"], conflict["cloud_ts"]
                        )
                    
                    log.dual_log(tag="Backup:Sync:Verdict", message="Conflict resolved", payload={"table": table_name, "id": conflict["id"], "verdict": verdict, "conflict_data": conflict})

                    if verdict == "backup":
                        row = bk_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} = ?", (conflict["id"],)).fetchone()
                        cols = [desc[1] for desc in bk_conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
                        op_transactions.append((f"INSERT OR REPLACE INTO {table_name} ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})", tuple(row)))
                        results["op_restored"] += 1
                    elif verdict == "operational":
                        row = op_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} = ?", (conflict["id"],)).fetchone()
                        cols = [desc[1] for desc in op_conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
                        enqueue_backup_write(f"INSERT OR REPLACE INTO {table_name} ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})", tuple(row))
                        results["bk_persisted"] += 1
                    elif verdict == "skip":
                        enqueue_backup_write("INSERT OR REPLACE INTO dead_letter_queue (dlq_id, table_name, row_id, row_data, error_message) VALUES (?, ?, ?, ?, ?)", (str(time.time()), table_name, conflict["id"], "CONFLICT", "Manually skipped via HITL"))
                    results["conflicts"] += 1

            if op_transactions:
                log.dual_log(tag="Backup:Sync:OpWriteRequest", message="Enqueuing Operational DB transactions", payload={"transaction_count": len(op_transactions)})
                enqueue_transaction(op_transactions)
                log.dual_log(tag="Backup:Sync:OpWriteResponse", message="Operational DB transactions enqueued", payload={"status": "success"})

        finally:
            try: bk_conn.close()
            except Exception: pass

        log.dual_log(tag="Backup:Sync:DrainWait", message="Waiting for backup writer queue to drain before cloud push", payload={"pending_tasks": backup_write_queue.unfinished_tasks})
        drain_start = time.monotonic()
        while backup_write_queue.unfinished_tasks > 0:
            if time.monotonic() - drain_start > 60.0:
                log.dual_log(tag="Backup:Sync:DrainTimeout", message="Queue drain timed out after 60s", level="WARNING", payload={"remaining": backup_write_queue.unfinished_tasks})
                break
            time.sleep(0.1)
        log.dual_log(tag="Backup:Sync:DrainComplete", message="Backup writer queue drained", payload={"elapsed_s": time.monotonic() - drain_start})

        if self.cloud.settings.enabled:
            log.dual_log(tag="Backup:Sync:CloudPushRequest", message="Pushing synchronized local data to cloud", payload={"tables": list(tables.keys())})
            try:
                results["cloud_push"] = self.cloud.sync_data(self.local.db_path, tables, batch_size=self.settings.sync.batch_size)
                log.dual_log(tag="Backup:Sync:CloudPushResponse", message="Cloud push complete", payload={"results": results["cloud_push"]})
            except Exception as e:
                log.dual_log(tag="Backup:Sync:CloudPushError", message=f"Cloud push failed: {e}", level="ERROR", payload={"error": str(e)})

        results["duration"] = time.time() - start_time
        log.dual_log(tag="Backup:Sync:End", message="Bidirectional Sync Pipeline Complete", payload=results)
        return results
