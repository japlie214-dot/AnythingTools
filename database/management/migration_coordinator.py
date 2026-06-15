# database/management/migration_coordinator.py
import sqlite3
import time
from typing import List
from database.management.migration_types import TypeMismatchPlan, MigrationRecord, MigrationPhase, MigrationStatus
from database.schemas import PERSISTED_TABLES
from database.schemas.column_defaults import get_filler
from database.management.schema_introspector import _get_columns, _columns_from_ddl_in_memory, _extract_default_from_ddl, _normalize_type_affinity
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class DualDBMigrationCoordinator:
    """Orchestrates operational-first, then-cloud migration sequence."""
    
    def __init__(self, op_conn: sqlite3.Connection, label: str):
        self.op_conn = op_conn
        self.label = label
        self.records: List[MigrationRecord] = []

    def execute(self, plans: List[TypeMismatchPlan]) -> List[MigrationRecord]:
        """Execute plans in PERSISTED_TABLES order."""
        order = {t: i for i, t in enumerate(PERSISTED_TABLES)}
        sorted_plans = sorted(plans, key=lambda p: order.get(p.table_name, 999))
        
        for plan in sorted_plans:
            record = self._migrate_operational(plan)
            self.records.append(record)
            
            if record.status == MigrationStatus.FAILED:
                log.dual_log(
                    tag="Migration:Operational:Failed",
                    level="CRITICAL",
                    message=f"[{self.label}] Operational migration failed for {plan.table_name}. Aborting.",
                    payload={"table": plan.table_name, "error": record.error_message}
                )
                break
                
            if record.status == MigrationStatus.SUCCESS:
                pass # Cloud engine sync handles the cloud portion elsewhere safely via SyncEngine
                
        return self.records

    def _migrate_operational(self, plan: TypeMismatchPlan) -> MigrationRecord:
        record = MigrationRecord(
            table_name=plan.table_name,
            phase=MigrationPhase.CLONE,
            clone_table_name=plan.clone_table_name
        )
        try:
            record.status = MigrationStatus.RUNNING
            record.start_time = time.monotonic()
            
            self._clone_table(plan)
            log.dual_log(tag="Migration:Clone", level="INFO", message=f"[{self.label}] Cloned {plan.table_name} to {plan.clone_table_name}", payload={"table": plan.table_name})
            
            self._recreate_table(plan)
            log.dual_log(tag="Migration:Recreate", level="INFO", message=f"[{self.label}] Recreated {plan.table_name}", payload={"table": plan.table_name})
            
            rows = self._repopulate_from_clone(plan)
            log.dual_log(tag="Migration:Repopulate", level="INFO", message=f"[{self.label}] Repopulated {rows} rows for {plan.table_name}", payload={"table": plan.table_name, "rows": rows})
            
            filled = self._autofill_skipped_columns(plan)
            log.dual_log(tag="Migration:Autofill", level="INFO", message=f"[{self.label}] Auto-filled {filled} columns for {plan.table_name}", payload={"table": plan.table_name, "filled_columns": filled})
            
            self._validate_operational(plan)
            self.op_conn.commit() # Commit successful migration
            
            record.status = MigrationStatus.SUCCESS
            record.rows_affected = rows
        except Exception as e:
            record.status = MigrationStatus.FAILED
            record.error_message = str(e)
            self._rollback_from_clone(plan)
            record.status = MigrationStatus.ROLLED_BACK
        finally:
            record.end_time = time.monotonic()
            return record

    def _clone_table(self, plan: TypeMismatchPlan):
        self.op_conn.execute(f"ALTER TABLE {plan.table_name} RENAME TO {plan.clone_table_name}")

    def _recreate_table(self, plan: TypeMismatchPlan):
        self.op_conn.executescript(plan.new_ddl)

    def _repopulate_from_clone(self, plan: TypeMismatchPlan) -> int:
        clone_cols = {c.name.lower() for c in _get_columns(self.op_conn, plan.clone_table_name)}
        new_cols = {c.name.lower() for c in _get_columns(self.op_conn, plan.table_name)}
        desired_cols = _columns_from_ddl_in_memory(plan.new_ddl, plan.table_name) or []
        skip_set = {c.lower() for c in plan.columns_to_skip}
        
        shared_cols = [c for c in (clone_cols & new_cols) if c not in skip_set]
        ordered_new = [c.name.lower() for c in _get_columns(self.op_conn, plan.table_name)]
        
        insert_cols = []
        select_cols = []
        
        for c in ordered_new:
            if c in shared_cols:
                insert_cols.append(c)
                select_cols.append(c)
            elif c in skip_set:
                d_col = next((dc for dc in desired_cols if dc.name.lower() == c), None)
                if d_col and d_col.notnull:
                    default_val = _extract_default_from_ddl(plan.new_ddl, plan.table_name, c)
                    if default_val is None:
                        aff = _normalize_type_affinity(d_col.type)
                        default_val = "0" if aff in ("INTEGER", "REAL", "NUMERIC") else "''"
                    insert_cols.append(c)
                    select_cols.append(f"{default_val} AS {c}")
                    
        if not insert_cols:
            return 0
            
        insert_str = ", ".join(insert_cols)
        select_str = ", ".join(select_cols)
        sql = f"INSERT INTO {plan.table_name} ({insert_str}) SELECT {select_str} FROM {plan.clone_table_name}"
        cursor = self.op_conn.execute(sql)
        return cursor.rowcount

    def _autofill_skipped_columns(self, plan: TypeMismatchPlan) -> int:
        total_filled = 0
        for col_name in plan.columns_to_skip:
            filler = get_filler(plan.table_name, col_name)
            if filler:
                try:
                    total_filled += filler(self.op_conn, plan.table_name, col_name)
                except Exception as e:
                    log.dual_log(tag="Migration:AutofillError", level="WARNING", message=f"Auto-fill failed for {plan.table_name}.{col_name}: {e}", payload={"table": plan.table_name, "error": str(e)})
        return total_filled

    def _validate_operational(self, plan: TypeMismatchPlan):
        actual = {c.name.lower(): c for c in _get_columns(self.op_conn, plan.table_name)}
        desired = _columns_from_ddl_in_memory(plan.new_ddl, plan.table_name)
        if not desired: return
        for d_col in desired:
            name = d_col.name.lower()
            if name not in actual:
                raise RuntimeError(f"Validation failed: column {name} missing after migration")
            if _normalize_type_affinity(actual[name].type) != _normalize_type_affinity(d_col.type):
                raise RuntimeError(f"Validation failed: {name} type still wrong")

    def _rollback_from_clone(self, plan: TypeMismatchPlan):
        try:
            self.op_conn.execute(f"DROP TABLE IF EXISTS {plan.table_name}")
            self.op_conn.execute(f"ALTER TABLE {plan.clone_table_name} RENAME TO {plan.table_name}")
            self.op_conn.commit()
            log.dual_log(tag="Migration:Rollback", level="WARNING", message=f"[{self.label}] Rolled back {plan.table_name} from clone", payload={"table": plan.table_name})
        except Exception as e:
            log.dual_log(tag="Migration:RollbackFailed", level="CRITICAL", message=f"[{self.label}] Rollback FAILED for {plan.table_name}: {e}", payload={"table": plan.table_name, "error": str(e)})
