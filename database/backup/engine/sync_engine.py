# database/backup/engine/sync_engine.py
"""Unified backup engine: Direct Operational DB  Snowflake sync.

Inline cloud writes (from enqueue_write) are handled by the CloudWriter
thread. This engine handles:
  1. Full/delta sync (operational  Snowflake)
  2. Bidirectional sync with HITL conflict resolution
  3. Restore from Snowflake  operational DB
  4. Periodic sync as safety net for missed inline writes
"""

# json: for lossless composite-PK serialization (replaces pipe-delimited strings).
# Ref: RFC 8259 — https://datatracker.ietf.org/doc/html/rfc8259
import json
import sqlite3
import time
import os
import tempfile
from database.backup.settings import BackupSettings
from database.backup.engine.cloud_engine import CloudEngine
from database.backup.schema_registry import BackupSchemaRegistry
from database.backup.sync.diff_engine import DiffEngine
from database.backup.sync.resolution import ConflictResolver, UserConfirmationHandler
from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)
from database.backup.models import SyncDecision
from sqlalchemy import text


class SyncEngine:
    """Direct Operational DB  Snowflake sync engine.
    
    Replaces the three-tier (Operational  backup.db  Snowflake) with
    a two-tier (Operational  Snowflake) architecture.
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

    def _get_table_watermark(self, op_conn: sqlite3.Connection, table_name: str) -> str:
        """Query the operational DB's sync ledger to find the last successful completed timestamp."""
        try:
            cursor = op_conn.execute(
                "SELECT max(completed_at) FROM sync_ledger WHERE state = 'COMPLETED' AND (table_name = ? OR table_name = 'ALL')",
                (table_name,)
            )
            val = cursor.fetchone()[0]
            return val if val else "1970-01-01T00:00:00"
        except sqlite3.OperationalError:
            return "1970-01-01T00:00:00"

    def compute_local_proofs(self, tables: dict) -> dict:
        proofs = {}
        try:
            op_conn = DatabaseManager.get_read_connection()
            for table_name, ddl in tables.items():
                if 'VIRTUAL' in ddl.upper():
                    continue
                try:
                    row_count = op_conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                except Exception:
                    row_count = 0
                watermark = self._get_table_watermark(op_conn, table_name)
                proofs[table_name] = {
                    "total_rows": row_count,
                    "watermark": watermark,
                    "has_updated_at": "updated_at" in ddl.lower()
                }
        except Exception as e:
            log.dual_log(tag="Backup:Proofs:LocalError", message=f"Could not compute local proofs: {e}", level="WARNING", payload={"error": str(e)})
        return proofs

    def compute_cloud_proofs(self, tables: dict) -> dict:
        if not self.cloud.settings.enabled:
            return {}
        proofs = {}
        try:
            def _count_cloud_rows():
                nonlocal proofs
                with self.cloud.engine.begin() as conn:
                    for table_name in tables:
                        if 'VIRTUAL' in tables[table_name].upper():
                            continue
                        try:
                            result = conn.execute(text(f"SELECT COUNT(*) FROM {self.cloud.settings.schema_name}.{table_name}"))
                            proofs[table_name] = {"total_rows": result.fetchone()[0]}
                        except Exception:
                            proofs[table_name] = {"total_rows": -1, "error": "table_not_found"}
                return proofs
            return self.cloud.circuit_breaker_pull.call(_count_cloud_rows)
        except Exception as e:
            log.dual_log(tag="Backup:Proofs:CloudError", message=f"Could not compute cloud proofs: {e}", level="WARNING", payload={"error": str(e)})
            return {}

    def decide_startup_action(self, local_proofs: dict, cloud_proofs: dict) -> SyncDecision:
        if not cloud_proofs:
            return SyncDecision(
                action="skip",
                reason="Cloud proofs unavailable - operating in degraded mode",
                local_proofs=local_proofs,
                cloud_proofs={},
                divergence_detected=False,
                hitl_required=False,
                recommended_strategy="operational_wins"
            )
        
        local_has_data = any(p.get("total_rows", 0) > 0 for p in local_proofs.values())
        cloud_has_data = any(p.get("total_rows", 0) > 0 for p in cloud_proofs.values())
        
        if not local_has_data and not cloud_has_data:
            return SyncDecision(
                action="skip",
                reason="Both local and cloud are empty - no data to sync",
                local_proofs=local_proofs,
                cloud_proofs=cloud_proofs,
                divergence_detected=False,
                hitl_required=False,
                recommended_strategy="operational_wins"
            )
            
        if not local_has_data and cloud_has_data:
            cloud_total = sum(p.get("total_rows", 0) for p in cloud_proofs.values() if p.get("total_rows", 0) > 0)
            return SyncDecision(
                action="pull_only",
                reason=f"Local DB is empty but cloud has {cloud_total} rows",
                local_proofs=local_proofs,
                cloud_proofs=cloud_proofs,
                divergence_detected=True,
                hitl_required=True,
                recommended_strategy="cloud_wins"
            )
            
        if local_has_data and not cloud_has_data:
            local_total = sum(p.get("total_rows", 0) for p in local_proofs.values())
            return SyncDecision(
                action="push_only",
                reason=f"Local has {local_total} rows but cloud is empty - populating fresh cloud",
                local_proofs=local_proofs,
                cloud_proofs=cloud_proofs,
                divergence_detected=False,
                hitl_required=False,
                recommended_strategy="operational_wins"
            )
            
        divergence_tables = []
        for t in local_proofs:
            l_rows = local_proofs[t].get("total_rows", 0)
            c_rows = cloud_proofs.get(t, {}).get("total_rows", 0)
            if l_rows != c_rows:
                divergence_tables.append(t)
                
        if not divergence_tables:
            return SyncDecision(
                action="skip",
                reason="Local and cloud row counts match - no divergence detected",
                local_proofs=local_proofs,
                cloud_proofs=cloud_proofs,
                divergence_detected=False,
                hitl_required=False,
                recommended_strategy="operational_wins"
            )
            
        return SyncDecision(
            action="bidirectional",
            reason=f"Row count divergence in tables: {', '.join(divergence_tables)}",
            local_proofs=local_proofs,
            cloud_proofs=cloud_proofs,
            divergence_detected=True,
            hitl_required=True,
            recommended_strategy="newest_overall_wins"
        )

    def _validate_post_sync(self, action: str, tables: dict, expected_cloud_proofs: dict) -> None:
        try:
            op_conn = DatabaseManager.get_read_connection()
            actual_counts = {}
            for table_name in tables:
                if 'VIRTUAL' in tables[table_name].upper():
                    continue
                try:
                    count = op_conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                    actual_counts[table_name] = count
                except Exception:
                    actual_counts[table_name] = -1

            log.dual_log(
                tag="Backup:Sync:Validation",
                message=f"Post-sync validation for '{action}' action",
                level="INFO",
                payload={
                    "action": action,
                    "actual_local_counts": actual_counts,
                    "expected_cloud_counts": {t: p.get("total_rows", -1) for t, p in expected_cloud_proofs.items()},
                }
            )
        except Exception as e:
            log.dual_log(
                tag="Backup:Sync:ValidationError",
                message=f"Post-sync validation failed: {e}",
                level="WARNING",
                payload={"error": str(e)}
            )
    
    def sync_startup(self) -> SyncDecision:
        from config import DATABASE_STAGING_ENABLED
        if DATABASE_STAGING_ENABLED:
            log.dual_log(
                tag="Backup:Sync:StagingSkip",
                message="Staging mode — skipping sync_startup",
                level="INFO",
            )
            return SyncDecision(action="skip", reason="staging mode - sync disabled")

        """Smart startup orchestration.

        This routine intentionally runs during process startup and may make
        authoritative decisions that change local operational state (e.g.
        restoring from cloud, or running bidirectional reconciliation with
        HITL). These actions are gated by the decision engine and, when
        required, operator confirmation via HITL prompts.

        IMPORTANT: Do not reuse this routine for shutdown-time syncs. Shutdown
        must remain best-effort and non-destructive — it only flushes queued
        writes and must never trigger pulls/restores which can block the
        shutdown path or mutate local state unexpectedly.
        """
        start_time = time.time()
        tables = BackupSchemaRegistry.get_expected_sqlite_tables()
        local_proofs = self.compute_local_proofs(tables)
        cloud_proofs = self.compute_cloud_proofs(tables)
        decision = self.decide_startup_action(local_proofs, cloud_proofs)

        log.dual_log(
            tag="Backup:Startup:Decision",
            message=f"Startup sync decision: {decision.action} - {decision.reason}",
            level="INFO" if not decision.divergence_detected else "WARNING",
            payload={
                "action": decision.action,
                "reason": decision.reason,
                "divergence_detected": decision.divergence_detected,
                "hitl_required": decision.hitl_required,
                "local_proofs": local_proofs,
                "cloud_proofs": cloud_proofs,
            }
        )

        if decision.action == "skip":
            decision.duration_seconds = time.time() - start_time
            return decision

        if decision.action == "push_only":
            self.sync_all(mode="delta")
            decision.duration_seconds = time.time() - start_time
            return decision

        if decision.action == "pull_only":
            if decision.hitl_required:
                strategy = self._hitl_startup_prompt(decision)
                decision.hitl_outcome = strategy
                if strategy == "abort":
                    decision.action = "abort"
                elif strategy in ("cloud_wins", "newest_overall_wins"):
                    restore_ok = self.restore_pipeline()
                    if restore_ok:
                        self._validate_post_sync("pull_only", tables, cloud_proofs)
            decision.duration_seconds = time.time() - start_time
            return decision

        if decision.action == "bidirectional":
            if decision.hitl_required:
                strategy = self._hitl_startup_prompt(decision)
                decision.hitl_outcome = strategy
                if strategy == "abort":
                    decision.action = "abort"
                else:
                    self.sync_bidirectional(mode="delta", default_strategy=strategy)
                    self._validate_post_sync("bidirectional", tables, cloud_proofs)
            decision.duration_seconds = time.time() - start_time
            return decision

    def _hitl_startup_prompt(self, decision: SyncDecision) -> str:
        from database.backup.sync.resolution import UserConfirmationHandler
        metrics = {"tables": {}}
        for t in decision.local_proofs:
            l_rows = decision.local_proofs[t].get("total_rows", 0)
            c_rows = decision.cloud_proofs.get(t, {}).get("total_rows", 0)
            if l_rows != c_rows or c_rows > 0:
                conflict_count = abs(l_rows - c_rows) if (l_rows > 0 and c_rows > 0) else 0
                metrics["tables"][t] = {
                    "op_only": max(0, l_rows - c_rows),
                    "cloud_only": max(0, c_rows - l_rows),
                    "content_identical": [],
                    "genuine_conflicts": [{}] * conflict_count,
                    "timestamp_drift": []
                }
        return UserConfirmationHandler.hitl_prompt_sync_strategy(metrics)

    def sync_all(self, mode: str = "delta") -> dict:
        from config import DATABASE_STAGING_ENABLED
        if DATABASE_STAGING_ENABLED:
            log.dual_log(
                tag="Backup:Sync:StagingSkip",
                message="Staging mode — skipping sync_all",
                level="INFO",
            )
            return {"skipped": True, "reason": "staging mode"}

        """Sync operational DB directly to Snowflake (no backup.db intermediary)."""
        from database.connection import DB_PATH, DatabaseManager
        
        start_time = time.time()
        tables = BackupSchemaRegistry.get_expected_sqlite_tables()
        results = {"cloud": {}, "duration": 0.0}

        # Proactive Analysis: Gather proof of what is available to sync
        sync_proofs = {}
        total_pending_rows = 0
        
        try:
            op_conn = DatabaseManager.get_read_connection()
            for table_name, ddl in tables.items():
                if 'VIRTUAL' in ddl.upper():
                    continue
                    
                # Get local row count
                try:
                    row_count = op_conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                except Exception:
                    row_count = 0
                    
                # Get watermark
                watermark = "1970-01-01T00:00:00"
                if mode == "delta":
                    watermark = self._get_table_watermark(op_conn, table_name)
                
                # Get count of pending updates
                ts_col = "updated_at" if "updated_at" in ddl.lower() else None
                pending_count = 0
                if ts_col:
                    try:
                        pending_count = op_conn.execute(
                            f"SELECT COUNT(*) FROM {table_name} WHERE {ts_col} > ?",
                            (watermark,)
                        ).fetchone()[0]
                    except Exception:
                        pending_count = row_count
                else:
                    pending_count = row_count
                    
                sync_proofs[table_name] = {
                    "total_local_rows": row_count,
                    "watermark_evaluated": watermark,
                    "pending_delta_rows": pending_count
                }
                total_pending_rows += pending_count
        except Exception as e:
            log.dual_log(
                tag="Backup:SyncAll:AnalysisFailed",
                message=f"Could not analyze operational state before sync: {e}",
                level="WARNING",
                payload={"error": str(e)}
            )

        if not self.cloud.settings.enabled:
            log.dual_log(
                tag="Backup:SyncAll:Skipped",
                message="SyncAll skipped: Cloud backup is disabled",
                level="INFO",
                payload={"reason": "cloud_disabled", "proofs": sync_proofs}
            )
            return {"status": "disabled"}

        # Log pre-sync state
        log.dual_log(
            tag="Backup:SyncAll:PreFlight",
            message=f"Pre-flight analysis: {total_pending_rows} pending rows across {len(sync_proofs)} tables",
            level="INFO",
            payload={"mode": mode, "total_pending_rows": total_pending_rows, "proofs": sync_proofs}
        )

        if total_pending_rows == 0:
            log.dual_log(
                tag="Backup:SyncAll:Skipped",
                message="SyncAll skipped: No new operational records found above watermarks",
                level="INFO",
                payload={"reason": "no_pending_changes", "proofs": sync_proofs}
            )
            results["duration"] = time.time() - start_time
            return results

        # Perform the actual push
        try:
            results["cloud"] = self.cloud.sync_data(
                str(DB_PATH), tables,
                batch_size=self.settings.sync.batch_size,
                delta_only=(mode == "delta")
            )
            
            pushed_total = sum(results["cloud"].values())
            
            # Post-sync ledger update to advance local watermarks
            try:
                from database.backup.sync.foundation import SyncLedger
                from database.writer import enqueue_write
                now_iso = SyncLedger.now_iso()
                enqueue_write(
                    "INSERT OR REPLACE INTO sync_ledger (operation_id, table_name, direction, row_count, state, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (now_iso, "ALL", "LOCAL_TO_CLOUD", pushed_total, "COMPLETED", now_iso),
                    track=True
                )
            except Exception as e_ledger:
                log.dual_log(tag="Backup:SyncAll:LedgerError", message=f"Failed to advance local sync ledger: {e_ledger}", level="WARNING", payload={"error": str(e_ledger)})

            log.dual_log(
                tag="Backup:SyncAll:Pushed",
                message=f"SyncAll pushed {pushed_total} rows to Snowflake",
                level="INFO",
                payload={"pushed_details": results["cloud"], "proofs": sync_proofs}
            )
        except Exception as e:
            log.dual_log(
                tag="Backup:SyncAll:CloudError",
                message=f"Cloud sync execution failed: {str(e)}",
                level="ERROR",
                payload={"error": str(e), "proofs": sync_proofs}
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
                level="WARNING",
                payload={"reason": "cloud_not_configured"}
            )
            return False
            
        from database.connection import DatabaseManager
        from database.schemas import PERSISTED_TABLES
        from database.backup.schema_registry import BackupSchemaRegistry
        from database.writer import enqueue_transaction
        
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
                            cursor = temp_conn.execute(f"SELECT * FROM {t}")
                            cols = [desc[0] for desc in temp_conn.execute(f"SELECT * FROM {t} LIMIT 1").description]
                            placeholders = ",".join(["?"] * len(cols))
                            insert_sql = f"INSERT OR REPLACE INTO {t} ({','.join(cols)}) VALUES ({placeholders})"
                            while True:
                                chunk = cursor.fetchmany(1000)
                                if not chunk:
                                    break
                                op_transactions = [(insert_sql, tuple(r)) for r in chunk]
                                receipt = enqueue_transaction(op_transactions, track=True)
                                if receipt:
                                    receipt.wait(timeout=60.0)
                        except Exception as e:
                            log.dual_log(
                                tag="Backup:Restore:TableError",
                                message=f"Failed to restore table {t}: {e}",
                                level="WARNING",
                                payload={"table": t, "error": str(e)}
                            )
                
                # Rebuild FTS5
                try:
                    from database.writer import enqueue_write
                    receipt = enqueue_write("INSERT INTO scraped_articles_fts(scraped_articles_fts) VALUES('rebuild')", track=True)
                    if receipt:
                        receipt.wait(timeout=180.0)
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

        import tempfile
        import os
        
        temp_fd, temp_path = tempfile.mkstemp(suffix=".db", prefix="sync_")
        os.close(temp_fd)
        temp_conn = sqlite3.connect(temp_path, timeout=30.0)
        op_conn = DatabaseManager.get_read_connection()

        metrics = {
            "op_db_path": str(DB_PATH),
            "cloud_account": self.cloud.settings.account if self.cloud.settings.enabled else "N/A",
            "cloud_enabled": self.cloud.settings.enabled,
            "tables": {}
        }

        triad_deltas = {}
        try:
            for t_name, ddl in tables.items():
                if 'VIRTUAL' not in ddl.upper():
                    temp_conn.executescript(ddl)
            if self.cloud.settings.enabled:
                self.cloud.pull_to_local(temp_path, tables)

            for table_name, ddl in tables.items():
                if 'VIRTUAL' in ddl.upper() or 'updated_at' not in ddl.lower():
                    continue
                try:
                    op_count = op_conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                    op_latest = op_conn.execute(f"SELECT MAX(updated_at) FROM {table_name}").fetchone()[0]
                except Exception:
                    op_count, op_latest = 0, "N/A"

                deltas = DiffEngine.compute_deltas(op_conn, temp_conn, table_name)
                triad_deltas[table_name] = deltas
                
                metrics["tables"][table_name] = {
                    "op_rows": op_count, "op_latest": op_latest,
                    "op_only": len(deltas["op_only"]), "cloud_only": len(deltas["cloud_only"]),
                    "conflicts": len(deltas.get("genuine_conflicts", [])),
                }
            
            selected_strategy = default_strategy
            hitl_bypassed = False
            hitl_bypass_reason = None
            
            # Evaluate auto-accept condition
            total_conflicts = sum(m.get("conflicts", 0) for m in metrics["tables"].values())
            total_cloud_only = sum(m.get("cloud_only", 0) for m in metrics["tables"].values())
            
            if not self.settings.hitl.interactive:
                hitl_bypassed = True
                hitl_bypass_reason = "hitl.interactive configuration is set to False"
            elif self.settings.hitl.auto_accept_on_no_conflict and total_conflicts == 0 and total_cloud_only == 0:
                hitl_bypassed = True
                hitl_bypass_reason = "auto_accept_on_no_conflict is True and zero conflicts/cloud-only records exist"
                selected_strategy = "operational_wins"
            
            if not hitl_bypassed:
                selected_strategy = UserConfirmationHandler.hitl_prompt_sync_strategy(metrics)
            
            if selected_strategy == 'abort':
                return {"status": "aborted"}

            log.dual_log(
                tag="Backup:Sync:Strategy",
                message=f"Strategy: {selected_strategy}" + (f" (HITL bypassed: {hitl_bypass_reason})" if hitl_bypassed else " (Selected via HITL)"),
                payload={
                    "strategy": selected_strategy,
                    "hitl_interactive_flag": self.settings.hitl.interactive,
                    "hitl_auto_accept_flag": self.settings.hitl.auto_accept_on_no_conflict,
                    "total_conflicts": total_conflicts,
                    "total_cloud_only": total_cloud_only,
                    "hitl_bypassed": hitl_bypassed,
                    "hitl_bypass_reason": hitl_bypass_reason
                }
            )

            from database.writer import enqueue_transaction
            op_transactions = []
            
            for table_name, deltas in triad_deltas.items():
                pk_col = deltas["pk_col"]
                
                if selected_strategy in ("cloud_wins", "newest_overall_wins") and deltas["cloud_only"]:
                    # Composite-PK-aware cloud→local restore.
                    # For single PK: WHERE pk_col IN (?,?,...)
                    # For composite PK: WHERE (c1=? AND c2=? AND ...) OR (c1=? AND c2=? AND ...)
                    pk_cols_list = [pk_col] if isinstance(pk_col, str) else list(pk_col)
                    chunk_size = 900 // max(1, len(pk_cols_list))
                    for chunk_idx in range(0, len(deltas["cloud_only"]), chunk_size):
                        chunk = deltas["cloud_only"][chunk_idx:chunk_idx+chunk_size]
                        if isinstance(pk_col, str):
                            # Single PK — simple IN clause
                            placeholders = ",".join(["?"] * len(chunk))
                            rows = temp_conn.execute(
                                f"SELECT * FROM {table_name} WHERE {pk_col} IN ({placeholders})",
                                chunk,
                            ).fetchall()
                        else:
                            # Composite PK — OR-of-ANDs pattern.
                            # Each cloud_only entry is a JSON-encoded list
                            # (e.g. '["AAPL","BS","Revenue|Sub","2024-Q1"]') so
                            # PK values containing "|" or any other special
                            # character survive the round-trip through
                            # DiffEngine._insert_diff_rows (which serializes
                            # via json.dumps). Ref: RFC 8259.
                            or_clauses = []
                            params = []
                            for pk_str in chunk:
                                try:
                                    pk_vals = json.loads(pk_str)
                                except (ValueError, TypeError):
                                    # Malformed entry — log and skip rather
                                    # than silently dropping via the old
                                    # len() guard. The previous behavior
                                    # would lose the row without a trace.
                                    log.dual_log(
                                        tag="Backup:Sync:MalformedPK",
                                        level="WARNING",
                                        message=f"Could not deserialize composite PK for {table_name} restore",
                                        payload={"table": table_name, "pk_repr": str(pk_str)[:200]},
                                    )
                                    continue
                                if len(pk_vals) == len(pk_col):
                                    and_parts = " AND ".join([f"{c} = ?" for c in pk_col])
                                    or_clauses.append(f"({and_parts})")
                                    params.extend(pk_vals)
                            if or_clauses:
                                where = " OR ".join(or_clauses)
                                rows = temp_conn.execute(
                                    f"SELECT * FROM {table_name} WHERE {where}",
                                    params,
                                ).fetchall()
                            else:
                                rows = []
                        if rows:
                            cols = [desc[0] for desc in temp_conn.execute(f"SELECT * FROM {table_name} LIMIT 1").description]
                            for r in rows:
                                op_transactions.append((f"INSERT OR REPLACE INTO {table_name} ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})", tuple(r)))
                                results["op_restored"] += 1

                if selected_strategy == "cloud_wins" and deltas["op_only"]:
                    for op_id in deltas["op_only"]:
                        if isinstance(pk_col, str):
                            op_transactions.append((f"DELETE FROM {table_name} WHERE {pk_col} = ?", (op_id,)))
                        else:
                            # Composite PK: op_id is a JSON-encoded list.
                            try:
                                pk_vals = json.loads(op_id)
                            except (ValueError, TypeError):
                                log.dual_log(
                                    tag="Backup:Sync:MalformedPK",
                                    level="WARNING",
                                    message=f"Could not deserialize composite PK for {table_name} delete",
                                    payload={"table": table_name, "pk_repr": str(op_id)[:200]},
                                )
                                continue
                            if len(pk_vals) == len(pk_col):
                                where = " AND ".join([f"{c} = ?" for c in pk_col])
                                op_transactions.append((f"DELETE FROM {table_name} WHERE {where}", tuple(pk_vals)))
                    results["op_deleted"] += len(deltas["op_only"])

                for conflict in deltas.get("genuine_conflicts", []):
                    verdict = ConflictResolver.resolve_conflict(conflict, strategy=selected_strategy)
                    if verdict == "manual":
                        verdict = UserConfirmationHandler.hitl_wait_for_sync_operator(table_name, conflict.get("id"), conflict.get("op_ts", ""), conflict.get("cloud_ts", ""))
                        
                    if verdict == "cloud":
                        conflict_id = conflict["id"]
                        if isinstance(pk_col, str):
                            row = temp_conn.execute(f"SELECT * FROM {table_name} WHERE {pk_col} = ?", (conflict_id,)).fetchone()
                        else:
                            try:
                                pk_vals = json.loads(conflict_id)
                            except (ValueError, TypeError):
                                log.dual_log(
                                    tag="Backup:Sync:MalformedPK",
                                    level="WARNING",
                                    message=f"Could not deserialize composite PK for {table_name} conflict",
                                    payload={"table": table_name, "pk_repr": str(conflict_id)[:200]},
                                )
                                continue
                            where = " AND ".join([f"{c} = ?" for c in pk_col])
                            row = temp_conn.execute(f"SELECT * FROM {table_name} WHERE {where}", tuple(pk_vals)).fetchone()
                        if row:
                            cols = [desc[0] for desc in temp_conn.execute(f"SELECT * FROM {table_name} LIMIT 1").description]
                            op_transactions.append((f"INSERT OR REPLACE INTO {table_name} ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})", tuple(row)))
                            results["op_restored"] += 1

            if op_transactions:
                enqueue_transaction(op_transactions)

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
            temp_conn.close()
            try:
                os.unlink(temp_path)
            except Exception:
                pass

        results["duration"] = time.time() - start_time
        return results
