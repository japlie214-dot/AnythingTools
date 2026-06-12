# database/management/reconciler.py
"""Agnostic Schema Reconciler for input-driven database management.

This reconciler operates exclusively on schemas passed as arguments:
- No hardcoded domain imports
- Granular validation with verbose logging
- Input-driven reconciliation for any SQLite file
"""

import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from database.management.schema_introspector import (
    schema_matches, table_exists, trigger_exists,
    _get_columns, _columns_from_ddl_in_memory, _normalize_type_affinity
)
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


@dataclass(frozen=True)
class ReconciliationAction:
    table_name: str
    action: str  # created | unchanged | altered | recreated | pruned
    is_master: bool = False
    reason: Optional[str] = None


@dataclass
class ReconciliationReport:
    actions: List[ReconciliationAction] = field(default_factory=list)
    master_tables_recreated: List[str] = field(default_factory=list)

    def add(self, action: ReconciliationAction) -> None:
        self.actions.append(action)
        if action.is_master and action.action == "recreated":
            self.master_tables_recreated.append(action.table_name)


class SchemaReconciler:
    """Pure engine that reconciles any database against provided schemas."""
    
    def __init__(
        self, 
        conn: sqlite3.Connection, 
        label: str,
        expected_tables: Dict[str, str],
        expected_triggers: Dict[str, str],
        master_tables: List[str]
    ):
        self.conn = conn
        self.label = label
        self.expected_tables = expected_tables
        self.expected_triggers = expected_triggers
        self.master_tables = master_tables

    def reconcile(self) -> ReconciliationReport:
        """Execute strict schema reconciliation with full observability."""
        report = ReconciliationReport()
        log.dual_log(tag="Database:Schema:ValidationStart", message=f"Starting Validation: {self.label}", payload={"label": self.label, "action": "validation_start"})
        
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            # 1. Prune unexpected tables
            self._prune_unexpected(report)
            
            # 2. Validate structures (tables + columns) and collect pending auto-fills
            pending_fills = self._validate_structures(report)
            
            # 3. Validate triggers (recreate dropped or missing triggers)
            self._validate_triggers(report)

            # 4. Execute auto-fill scripts (safe now that schema and triggers are finalized)
            self._run_pending_auto_fills(pending_fills, report)

            # 5. Verify vec0 readability
            self._verify_vec0_readability(report)
            
            # Summary Log
            added = sum(1 for a in report.actions if a.action == "altered" and "AddColumn" in (a.reason or ""))
            dropped = sum(1 for a in report.actions if a.action == "altered" and "DropColumn" in (a.reason or ""))
            recreated = sum(1 for a in report.actions if a.action == "recreated")
            log.dual_log(tag="Database:Schema:Summary", level="INFO", message=f"Schema reconciliation complete: {added} added, {dropped} dropped, {recreated} recreated", payload={"added": added, "dropped": dropped, "recreated": recreated})

            self.conn.commit()
            return report

        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    def _prune_unexpected(self, report: ReconciliationReport):
        """Drop tables not in expected schema with verbose logging."""
        expected = set(self.expected_tables.keys())
        
        existing = [
            r[0] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        
        for table in existing:
            # Skip sqlite_ tables
            if table.startswith("sqlite_"):
                continue
            
            # Check if it's a shadow table derived from any expected FTS/vec table
            is_shadow = False
            for base_table in self.expected_tables.keys():
                if table.startswith(f"{base_table}_"):
                    is_shadow = True
                    break

            if is_shadow:
                log.dual_log(tag="Database:Schema:PreserveShadow", message=f"[{self.label}] Preserving shadow: {table}", payload={"label": self.label, "table": table, "action": "preserve_shadow"})
                continue
            
            if table.startswith("sn_dt_"):
                log.dual_log(tag="Database:Schema:PreserveDynamic", message=f"[{self.label}] Preserving dynamic table: {table}", payload={"label": self.label, "table": table, "action": "preserve_dynamic"})
                continue
            
            if table not in expected:
                log.dual_log(tag="Database:Schema:DropUnexpected", level="WARNING", message=f"[{self.label}] Dropping unexpected table: {table}", payload={"label": self.label, "table": table, "action": "drop_unexpected"})
                self.conn.execute(f"DROP TABLE IF EXISTS {table}")
                report.add(ReconciliationAction(table, "pruned"))

    def _is_virtual_table(self, name: str) -> bool:
        """Check if a table is defined as VIRTUAL in sqlite_master."""
        res = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return res and "VIRTUAL" in (res[0] or "").upper()

    def _validate_structures(self, report: ReconciliationReport) -> dict:
        """Deep validation of tables and columns with granular logging."""
        pending_fills = {}
        for name, ddl in self.expected_tables.items():
            log.dual_log(tag="Database:Schema:CheckTable", message=f"[{self.label}] Checking table: {name}", payload={"label": self.label, "table": name})
            
            exists = table_exists(self.conn, name)
            is_virtual = self._is_virtual_table(name)
            
            if not exists:
                log.dual_log(tag="Database:Schema:CreateTable", message=f"[{self.label}] Table {name} missing. Creating.", payload={"label": self.label, "table": name, "action": "create"})
                self.conn.executescript(ddl)
                report.add(ReconciliationAction(name, "created"))
                continue

            # Virtual Table Immunity: Skip deep column validation
            if is_virtual:
                log.dual_log(tag="Database:Schema:VirtualImmunity", message=f"[{self.label}] {name}: Virtual table detected, skipping column check.", payload={"label": self.label, "table": name, "virtual": True})
                report.add(ReconciliationAction(name, "unchanged"))
                continue

            # Deep Column Check
            actual_cols = {c.name.lower(): c for c in _get_columns(self.conn, name)}
            desired_cols = {c.name.lower(): c for c in (_columns_from_ddl_in_memory(ddl, name) or [])}
            
            missing = [c for c in desired_cols if c not in actual_cols]
            type_mismatches = []
            constraint_mismatches = []
            
            for col_name, d_col in desired_cols.items():
                if col_name in actual_cols:
                    a_col = actual_cols[col_name]
                    if _normalize_type_affinity(a_col.type) != _normalize_type_affinity(d_col.type):
                        type_mismatches.append((col_name, a_col.type, d_col.type))
                    if a_col.notnull != d_col.notnull:
                        constraint_mismatches.append((col_name, 'NOT NULL', d_col.notnull))
                    if a_col.pk != d_col.pk:
                        constraint_mismatches.append((col_name, 'PRIMARY KEY', d_col.pk))
            
            extra = set(actual_cols.keys()) - set(desired_cols.keys())
            
            altered = False
            
            if missing:
                if self._add_missing_columns(name, ddl, missing, desired_cols, report):
                    altered = True
                    pending_fills[name] = missing

            if extra:
                if self._drop_extra_columns(name, extra, report):
                    altered = True

            if type_mismatches or constraint_mismatches:
                is_master = name in self.master_tables
                if is_master:
                    self._snapshot_master(name)
                
                reason_parts = []
                if type_mismatches: reason_parts.append(f"type_drift: {len(type_mismatches)}")
                if constraint_mismatches: reason_parts.append(f"constraint_drift: {len(constraint_mismatches)}")
                reason = '; '.join(reason_parts)
                
                log.dual_log(tag="Database:Schema:RecreateTable", level="WARNING", message=f"[{self.label}] Recreating {name} due to: {reason}", payload={"label": self.label, "table": name, "reason": reason})
                self.conn.execute(f"DROP TABLE IF EXISTS {name}")
                self.conn.executescript(ddl)
                report.add(ReconciliationAction(name, "recreated", is_master, reason))
            elif altered:
                pass # report already updated in sub-methods
            else:
                log.dual_log(tag="Database:Schema:StructureValid", message=f"[{self.label}] {name}: Structure valid", payload={"label": self.label, "table": name, "status": "valid"})
                report.add(ReconciliationAction(name, "unchanged"))
                
        return pending_fills

    def _add_missing_columns(self, table_name: str, ddl: str, missing_cols: list, desired_cols: dict, report: ReconciliationReport) -> bool:
        from database.management.schema_introspector import _extract_default_from_ddl
        added = False
        for col_name in missing_cols:
            d_col = desired_cols[col_name]
            col_type = d_col.type or "TEXT"
            col_def = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"
            
            if d_col.notnull:
                default_val = _extract_default_from_ddl(ddl, table_name, col_name)
                if default_val is None:
                    affinity = _normalize_type_affinity(col_type)
                    if affinity == "INTEGER": default_val = "0"
                    elif affinity == "REAL": default_val = "0.0"
                    else: default_val = "''"
                col_def += f" NOT NULL DEFAULT {default_val}"
            elif d_col.dflt_value is not None:
                col_def += f" DEFAULT {d_col.dflt_value}"
                
            try:
                log.dual_log(tag="Database:Schema:AddColumn", level="INFO", message=f"[{self.label}] Adding column '{col_name}' to {table_name}", payload={"table": table_name, "column": col_name, "definition": col_def})
                self.conn.execute(col_def)
                added = True
                report.add(ReconciliationAction(table_name, "altered", reason=f"AddColumn:{col_name}"))
            except Exception as e:
                log.dual_log(tag="Database:Schema:AddColumnFailed", level="WARNING", message=f"[{self.label}] Failed to add column '{col_name}': {e}", payload={"table": table_name, "error": str(e)})
        return added

    def _drop_extra_columns(self, table_name: str, extra_cols: set, report: ReconciliationReport) -> bool:
        import sqlite3
        db_path = None
        for row in self.conn.execute("PRAGMA database_list").fetchall():
            if row[1] == "main":
                db_path = row[2]
                break

        dropped = False
        for col_name in extra_cols:
            backup_path = f"{db_path}.bak" if db_path else None
            if backup_path:
                try:
                    with sqlite3.connect(backup_path) as bck:
                        self.conn.backup(bck)
                except Exception as be:
                    log.dual_log(tag="Database:Schema:BackupFailed", level="WARNING", message=f"Failed to backup before drop: {be}", payload={"table": table_name})
                    backup_path = None

            try:
                # Drop dependent indexes first
                indexes = self.conn.execute(f"PRAGMA index_list({table_name})").fetchall()
                for idx in indexes:
                    idx_name = idx[1]
                    if idx_name.startswith("sqlite_autoindex"): continue
                    idx_cols = self.conn.execute(f"PRAGMA index_info({idx_name})").fetchall()
                    if any(c[2].lower() == col_name.lower() for c in idx_cols):
                        self.conn.execute(f"DROP INDEX IF EXISTS {idx_name}")
                        log.dual_log(tag="Database:Schema:DropIndex", level="INFO", message=f"Dropped dependent index '{idx_name}'", payload={"index": idx_name})

                log.dual_log(tag="Database:Schema:DropColumn", level="WARNING", message=f"[{self.label}] Dropping column '{col_name}' from {table_name}", payload={"table": table_name, "column": col_name})
                self.conn.execute(f"ALTER TABLE {table_name} DROP COLUMN {col_name}")
                dropped = True
                report.add(ReconciliationAction(table_name, "altered", reason=f"DropColumn:{col_name}"))
            except Exception as e:
                log.dual_log(tag="Database:Schema:DropColumnFailed", level="WARNING", message=f"[{self.label}] Cannot drop column '{col_name}' from {table_name}: {e}. Fallback to table recreation required.", payload={"table": table_name, "column": col_name, "error": str(e)})
                if backup_path:
                    try:
                        self.conn.rollback()
                        with sqlite3.connect(backup_path) as bck:
                            bck.backup(self.conn)
                    except Exception as re:
                        log.dual_log(tag="Database:Schema:RestoreFailed", level="ERROR", message=f"Failed to restore database from backup: {re}")
                    raise RuntimeError(f"ALTER TABLE DROP COLUMN failed on {table_name}.{col_name}. Reverted database to pre-drop backup.") from e
        return dropped

    def _run_pending_auto_fills(self, pending_fills: dict, report: ReconciliationReport) -> None:
        from database.schemas.column_defaults import get_filler
        for table_name, new_columns in pending_fills.items():
            for col_name in new_columns:
                filler = get_filler(table_name, col_name)
                if not filler:
                    continue
                try:
                    row_count = self.conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                    if row_count == 0:
                        continue
                    
                    log.dual_log(tag="Database:Schema:AutoFill", level="INFO", message=f"[{self.label}] Auto-filling column '{col_name}' in {table_name}", payload={"table": table_name, "column": col_name, "rows": row_count})
                    filled = filler(self.conn, table_name, col_name)
                    log.dual_log(tag="Database:Schema:AutoFillComplete", level="INFO", message=f"[{self.label}] Auto-filled {filled} rows", payload={"table": table_name, "filled": filled})
                except Exception as e:
                    log.dual_log(tag="Database:Schema:AutoFillError", level="WARNING", message=f"[{self.label}] Auto-fill failed for '{col_name}': {e}", payload={"table": table_name, "error": str(e)})

    def _validate_triggers(self, report: ReconciliationReport):
        """Validate triggers with logging."""
        for name, ddl in self.expected_triggers.items():
            exists = trigger_exists(self.conn, name)
            if not exists:
                log.dual_log(tag="Database:Schema:CreateTrigger", message=f"[{self.label}] Creating trigger: {name}", payload={"label": self.label, "trigger": name})
                self.conn.executescript(ddl)
                report.add(ReconciliationAction(name, "created"))
            else:
                log.dual_log(tag="Database:Schema:TriggerValid", message=f"[{self.label}] Trigger {name} valid", payload={"label": self.label, "trigger": name, "status": "valid"})
                report.add(ReconciliationAction(name, "unchanged"))

    def _nuke_vec0_table(self, table_name: str) -> None:
        """Force-remove a corrupted virtual table and its shadows bypassing xDestroy.

        Steps:
        1. Enumerate shadow tables while sqlite_master is consistent.
        2. Remove the virtual table row from sqlite_master using PRAGMA writable_schema = ON.
        3. Force SQLite to refresh its in-memory schema cache by creating and dropping a temp table.
        4. Finally, drop the shadow tables using safe DROP TABLE statements.
        """
        try:
            self.conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            return
        except Exception as e:
            log.dual_log(tag="Database:Schema:Nuke", level="WARNING", message=f"[{self.label}] Standard drop failed for {table_name}, initiating hard nuke.", payload={"error": str(e)})

        try:
            # 1. Gather shadow tables before modifying sqlite_master
            shadows = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? || '_%'", (table_name,)).fetchall()
            shadow_names = [row[0] for row in shadows]

            # 2. Excise the virtual table from sqlite_master
            self.conn.execute("PRAGMA writable_schema = ON")
            try:
                self.conn.execute("DELETE FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
            finally:
                try:
                    self.conn.execute("PRAGMA writable_schema = OFF")
                except Exception:
                    pass

            # 3. Force SQLite to reload the schema cache so it forgets the virtual table
            self.conn.execute("CREATE TABLE IF NOT EXISTS __schema_reload_temp (id INTEGER)")
            self.conn.execute("DROP TABLE IF EXISTS __schema_reload_temp")

            # 4. Safely drop the now-orphaned shadow tables using standard DROP TABLE
            for shadow_name in shadow_names:
                self.conn.execute(f"DROP TABLE IF EXISTS [{shadow_name}]")
        except Exception as nuke_err:
            raise RuntimeError(f"Nuke sequence failed for {table_name}: {nuke_err}")

    def _verify_vec0_readability(self, report: ReconciliationReport) -> None:
        """Read-only probe to verify vec0 health without causing WAL corruption."""
        for name, ddl in self.expected_tables.items():
            if not self._is_virtual_table(name) or "vec0" not in (ddl or "").lower():
                continue
            
            try:
                self.conn.execute(f"SELECT rowid FROM {name} LIMIT 1").fetchall()
            except Exception as e:
                log.dual_log(
                    tag="Database:Schema:Vec0ProbeFailed",
                    level="WARNING",
                    message=f"[{self.label}] {name}: vec0 read probe failed, table corrupted. Nuking.",
                    payload={"label": self.label, "table": name, "error": str(e)}
                )
                is_master = name in self.master_tables
                self._nuke_vec0_table(name)
                self.conn.executescript(ddl)
                report.add(ReconciliationAction(name, "recreated", is_master=is_master, reason=f"vec0 read probe failed: {e}"))

    def _snapshot_master(self, table_name: str):
        """Pre-drop snapshot for master tables by locally renaming them to prevent loss and avoid circular runner loops."""
        try:
            import time
            timestamp = int(time.time())
            backup_name = f"_old_{table_name}_{timestamp}"
            self.conn.execute(f"ALTER TABLE {table_name} RENAME TO {backup_name}")
            log.dual_log(
                tag="Database:Schema:LocalSnapshot",
                level="INFO",
                message=f"[{self.label}] Locally renamed master table {table_name} to {backup_name} before recreation",
                payload={"label": self.label, "table": table_name, "backup_table": backup_name}
            )
        except Exception as e:
            log.dual_log(
                tag="Database:Schema:SnapshotFailed",
                level="CRITICAL",
                message=f"[{self.label}] Local snapshot of master table {table_name} failed. Error: {e}",
                payload={"label": self.label, "table": table_name, "error": str(e)},
            )
