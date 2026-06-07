# database/backup/engine/dual_engine.py
from database.backup.settings import BackupSettings
from database.backup.engine.local_engine import LocalEngine
from database.backup.engine.cloud_engine import CloudEngine
from database.backup.writer.backup_writer import enqueue_backup_write
from utils.logger import get_dual_logger
import json

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

            if self.settings.vec0.enabled:
                try:
                    from database.backup.vec.adapter import VectorBackupAdapter
                    from database.connection import SQLITE_VEC_AVAILABLE, DatabaseManager
                    import sqlite3
                    if SQLITE_VEC_AVAILABLE:
                        op_conn = DatabaseManager.get_read_connection()
                        bk_conn = sqlite3.connect(self.local.db_path, timeout=30.0)
                        try:
                            v_count = VectorBackupAdapter.backup_vectors(op_conn, bk_conn)
                            log.dual_log(tag="Backup:Sync:VecBackup", message=f"Vector backup complete: {v_count} vectors", payload={"count": v_count})
                        finally:
                            bk_conn.close()
                except Exception as ve:
                    log.dual_log(tag="Backup:Sync:VecBackupError", message=f"Vector backup failed: {ve}", level="ERROR", payload={"error": str(ve)})
        except Exception as e:
            log.dual_log(tag="Backup:Sync:LocalError", message=f"Local sync failed: {str(e)}", level="ERROR", payload={"error": str(e)})
            results["local_error"] = str(e)
            return results

        if self.cloud.settings.enabled:
            try:
                # DELTA PRINCIPLE: Shutdown sync forces an operational_wins strategy
                # (via local SQLite sync) and then performs a delta-only cloud push to Snowflake.
                results["cloud"] = self.cloud.sync_data(self.local.db_path, tables, batch_size=self.settings.sync.batch_size, delta_only=True)
            except Exception as e:
                log.dual_log(tag="Backup:Sync:CloudError", message=f"Cloud sync failed: {str(e)}", level="ERROR", payload={"error": str(e)})
                results["cloud_error"] = str(e)

        results["duration"] = time.time() - start_time
        return results

    def restore_pipeline(self) -> bool:
        import sqlite3
        from database.backup.schema_registry import BackupSchemaRegistry
        from database.connection import DatabaseManager
        from database.backup.vec.adapter import VectorBackupAdapter
        from database.schemas import PERSISTED_TABLES
        
        op_conn = DatabaseManager.create_write_connection()
        bk_conn = sqlite3.connect(self.local.db_path)
        tables = BackupSchemaRegistry.get_expected_sqlite_tables()
        
        try:
            op_conn.execute("PRAGMA foreign_keys = OFF")
            
            # Order 1: PERSISTED_TABLES (scalar data)
            for t in PERSISTED_TABLES:
                if t in tables:
                    rows = bk_conn.execute(f"SELECT * FROM {t}").fetchall()
                    if rows:
                        cols = [desc[0] for desc in bk_conn.execute(f"SELECT * FROM {t} LIMIT 1").description]
                        placeholders = ",".join(["?"] * len(cols))
                        op_conn.execute(f"DELETE FROM {t}")
                        op_conn.executemany(f"INSERT INTO {t} ({','.join(cols)}) VALUES ({placeholders})", rows)
            
            # Order 2: FTS5 Triggers automatically fired during scalar insert.
            try:
                op_conn.execute("INSERT INTO scraped_articles_fts(scraped_articles_fts) VALUES('rebuild')")
            except Exception:
                pass
                
            # Order 3: Vector companion table to vec0
            VectorBackupAdapter.restore_vectors(bk_conn, op_conn)
            
            op_conn.commit()
            return True
        except Exception as e:
            log.dual_log(tag="Backup:Restore:Error", message=f"Restore pipeline failed: {e}", level="CRITICAL", payload={"error": str(e)})
            op_conn.rollback()
            return False
        finally:
            op_conn.execute("PRAGMA foreign_keys = ON")
            op_conn.close()
            bk_conn.close()

    def sync_bidirectional(self, mode: str = "delta", default_strategy: str = "newest_overall_wins") -> dict:
        import time
        import sqlite3
        from database.backup.schema_registry import BackupSchemaRegistry
        from database.backup.sync.diff_engine import DiffEngine
        from database.backup.sync.resolution import ConflictResolver, UserConfirmationHandler
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

                deltas = DiffEngine.compute_triad_deltas(op_conn, bk_conn, None, table_name)
                triad_deltas[table_name] = deltas
                
                metrics["tables"][table_name] = {
                    "op_rows": op_count, "bk_rows": bk_count, "op_latest": op_latest, "bk_latest": bk_latest,
                    "op_only": len(deltas["op_only"]), "bk_only": len(deltas["bk_only"]),
                    "conflicts": len(deltas.get("genuine_conflicts", [])),
                    "genuine_conflicts": deltas.get("genuine_conflicts", []),
                    "content_identical": deltas.get("content_identical", []),
                    "timestamp_drift": deltas.get("timestamp_drift", []),
                    "total_rows": deltas.get("total_rows", 0)
                }
            
            # HITL CONFIRMATION
            selected_strategy = UserConfirmationHandler.hitl_prompt_sync_strategy(metrics)
            if selected_strategy == 'abort':
                log.dual_log(tag="Backup:Sync:Abort", message="Sync aborted via HITL", level="WARNING", payload={"metrics": metrics})
                return {"status": "aborted"}

            log.dual_log(tag="Backup:Sync:Strategy", message=f"Strategy selected: {selected_strategy}", payload={"strategy": selected_strategy, "metrics": metrics})

            # EXECUTE SYNC
            from database.backup.writer.backup_writer import BackupWriteTask
            op_transactions = []
            ordered_tables = list(tables.keys())
            
            # Phase 1: DELETE operations (Reverse order for FK safety)
            for table_name in reversed(ordered_tables):
                if table_name not in triad_deltas: continue
                deltas = triad_deltas[table_name]
                pk_col = deltas["pk_col"]
                
                if selected_strategy == "operational_wins" and deltas["bk_only"]:
                    enqueue_backup_write(BackupWriteTask(table_name, "DELETE", [{pk_col: bk_id} for bk_id in deltas["bk_only"]], pk_col))
                    results["bk_deleted"] += len(deltas["bk_only"])
                    for bk_id in deltas["bk_only"]:
                        log.dual_log(tag="Backup:Sync:Verdict", message=f"[{table_name}:{bk_id}] action=DELETE_FROM_BK reason=operational_wins", payload={"table": table_name, "id": bk_id})
                elif selected_strategy in ("local_backup_wins", "backup_wins") and deltas["op_only"]:
                    for op_id in deltas["op_only"]:
                        op_transactions.append((f"DELETE FROM {table_name} WHERE {pk_col} = ?", (op_id,)))
                    results["op_deleted"] += len(deltas["op_only"])
                    for op_id in deltas["op_only"]:
                        log.dual_log(tag="Backup:Sync:Verdict", message=f"[{table_name}:{op_id}] action=DELETE_FROM_OP reason=local_backup_wins", payload={"table": table_name, "id": op_id})
            
            # Phase 2: UPSERT operations (Forward order)
            for table_name in ordered_tables:
                if table_name not in triad_deltas: continue
                deltas = triad_deltas[table_name]
                pk_col = deltas["pk_col"]
                
                if selected_strategy == "operational_wins" and deltas["op_only"]:
                    rows = op_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} IN ({','.join(['?']*len(deltas['op_only']))})", deltas["op_only"]).fetchall()
                    if rows:
                        cols = [desc[0] for desc in op_conn.execute(f"SELECT * FROM {table_name} LIMIT 1").description]
                        enqueue_backup_write(BackupWriteTask(table_name, "UPSERT", [dict(zip(cols, r)) for r in rows], pk_col))
                        results["bk_persisted"] += len(rows)
                        for op_id in deltas["op_only"]:
                            log.dual_log(tag="Backup:Sync:Verdict", message=f"[{table_name}:{op_id}] action=PERSIST_TO_BK reason=operational_wins", payload={"table": table_name, "id": op_id})
                elif selected_strategy in ("local_backup_wins", "backup_wins") and deltas["bk_only"]:
                    rows = bk_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} IN ({','.join(['?']*len(deltas['bk_only']))})", deltas["bk_only"]).fetchall()
                    if rows:
                        cols = [desc[0] for desc in bk_conn.execute(f"SELECT * FROM {table_name} LIMIT 1").description]
                        for r in rows:
                            op_transactions.append((f"INSERT OR REPLACE INTO {table_name} ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})", tuple(r)))
                        results["op_restored"] += len(rows)
                        for bk_id in deltas["bk_only"]:
                            log.dual_log(tag="Backup:Sync:Verdict", message=f"[{table_name}:{bk_id}] action=RESTORE_TO_OP reason=local_backup_wins", payload={"table": table_name, "id": bk_id})
                elif selected_strategy == "newest_overall_wins":
                    if deltas["bk_only"]:
                        rows = bk_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} IN ({','.join(['?']*len(deltas['bk_only']))})", deltas["bk_only"]).fetchall()
                        if rows:
                            cols = [desc[0] for desc in bk_conn.execute(f"SELECT * FROM {table_name} LIMIT 1").description]
                            for r in rows:
                                op_transactions.append((f"INSERT OR REPLACE INTO {table_name} ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})", tuple(r)))
                            results["op_restored"] += len(rows)
                            for bk_id in deltas["bk_only"]:
                                log.dual_log(tag="Backup:Sync:Verdict", message=f"[{table_name}:{bk_id}] action=RESTORE_TO_OP reason=merge_bk_only", payload={"table": table_name, "id": bk_id})
                    if deltas["op_only"]:
                        rows = op_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} IN ({','.join(['?']*len(deltas['op_only']))})", deltas["op_only"]).fetchall()
                        if rows:
                            cols = [desc[0] for desc in op_conn.execute(f"SELECT * FROM {table_name} LIMIT 1").description]
                            enqueue_backup_write(BackupWriteTask(table_name, "UPSERT", [dict(zip(cols, r)) for r in rows], pk_col))
                            results["bk_persisted"] += len(rows)
                            for op_id in deltas["op_only"]:
                                log.dual_log(tag="Backup:Sync:Verdict", message=f"[{table_name}:{op_id}] action=PERSIST_TO_BK reason=merge_op_only", payload={"table": table_name, "id": op_id})

                for conflict in deltas.get("genuine_conflicts", []):
                    verdict = ConflictResolver.resolve_triad(conflict, strategy=selected_strategy)
                    if verdict == "manual":
                        verdict = UserConfirmationHandler.hitl_wait_for_sync_operator(table_name, conflict.get("id"), conflict.get("op_ts", ""), conflict.get("bk_ts", ""), conflict.get("cloud_ts", ""))
                    
                    log.dual_log(tag="Backup:Sync:Verdict", message=f"[{table_name}:{conflict['id']}] action=CONFLICT_RESOLVED verdict={verdict} reason={selected_strategy}", payload={"table": table_name, "id": conflict["id"], "verdict": verdict})

                    if verdict == "backup":
                        row = bk_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} = ?", (conflict["id"],)).fetchone()
                        if row:
                            cols = [desc[0] for desc in bk_conn.execute(f"SELECT * FROM {table_name} LIMIT 1").description]
                            op_transactions.append((f"INSERT OR REPLACE INTO {table_name} ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})", tuple(row)))
                            results["op_restored"] += 1
                    elif verdict == "operational":
                        row = op_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} = ?", (conflict["id"],)).fetchone()
                        if row:
                            cols = [desc[0] for desc in op_conn.execute(f"SELECT * FROM {table_name} LIMIT 1").description]
                            enqueue_backup_write(BackupWriteTask(table_name, "UPSERT", [dict(zip(cols, row))], pk_col))
                            results["bk_persisted"] += 1
                    elif verdict == "skip":
                        enqueue_backup_write(BackupWriteTask("dead_letter_queue", "DLQ", [{pk_col: conflict["id"], "table_name": table_name, "row_data": json.dumps(conflict), "_error_msg": "Manually skipped via HITL"}], pk_col))
                    results["conflicts"] += 1

            if op_transactions:
                log.dual_log(tag="Backup:Sync:OpWriteRequest", message="Enqueuing Operational DB transactions", payload={"transaction_count": len(op_transactions)})
                enqueue_transaction(op_transactions)
                log.dual_log(tag="Backup:Sync:OpWriteResponse", message="Operational DB transactions enqueued", payload={"status": "success"})

            if self.settings.vec0.enabled:
                try:
                    from database.backup.vec.adapter import VectorBackupAdapter
                    from database.connection import SQLITE_VEC_AVAILABLE
                    if SQLITE_VEC_AVAILABLE:
                        v_count = VectorBackupAdapter.backup_vectors(op_conn, bk_conn)
                        log.dual_log(tag="Backup:Sync:VecBackup", message=f"Vector backup complete: {v_count} vectors", payload={"count": v_count})
                except Exception as ve:
                    log.dual_log(tag="Backup:Sync:VecBackupError", message=f"Vector backup failed: {ve}", level="ERROR", payload={"error": str(ve)})

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
        
        summary_lines = ["=== SYNC SUMMARY ==="]
        for tbl in sorted(triad_deltas.keys()):
            d = triad_deltas[tbl]
            summary_lines.append(f" {tbl}: op_only={len(d['op_only'])} bk_only={len(d['bk_only'])} conflicts={len(d.get('genuine_conflicts',[]))} identical={len(d.get('content_identical',[]))}")
        summary_lines.append(f"Total: op_restored={results['op_restored']} op_deleted={results['op_deleted']} bk_persisted={results['bk_persisted']} bk_deleted={results['bk_deleted']} conflicts={results['conflicts']}")
        summary_lines.append(f"Duration: {time.time() - start_time:.2f}s")
        log.dual_log(tag="Backup:Sync:Summary", message="Bidirectional sync pipeline complete", payload={"summary_text": "\n".join(summary_lines), "results": results})

        if self.cloud.settings.enabled:
            log.dual_log(tag="Backup:Sync:CloudPushRequest", message="Pushing synchronized local data to cloud (delta mode)", payload={"tables": list(tables.keys()), "mode": "delta"})
            try:
                # DELTA PRINCIPLE: Only push rows that differ between local backup and cloud via manifest diffing.
                results["cloud_push"] = self.cloud.sync_data(self.local.db_path, tables, batch_size=self.settings.sync.batch_size, delta_only=True)
                log.dual_log(tag="Backup:Sync:CloudPushResponse", message="Cloud push complete", payload={"results": results["cloud_push"]})
            except Exception as e:
                log.dual_log(tag="Backup:Sync:CloudPushError", message=f"Cloud push failed: {e}", level="ERROR", payload={"error": str(e)})

        results["duration"] = time.time() - start_time
        log.dual_log(tag="Backup:Sync:End", message="Bidirectional Sync Pipeline Complete", payload=results)
        return results
