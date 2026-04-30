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
        log.dual_log(tag="DB:Validate", message=f"Starting Validation: {self.label}", payload={"label": self.label, "action": "validation_start"})
        
        self.conn.execute("PRAGMA foreign_keys = OFF")
        
        try:
            # 1. Prune unexpected tables
            self._prune_unexpected(report)
            
            # 2. Validate structures (tables + columns)
            self._validate_structures(report)
            
            # 3. Validate triggers
            self._validate_triggers(report)
            
            self.conn.commit()
            return report
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    def _prune_unexpected(self, report: ReconciliationReport):
        """Drop tables not in expected schema with verbose logging."""
        expected = set(self.expected_tables.keys())
        
        # FTS5 and vec0 shadow suffixes (EXPANDED)
        shadow_suffixes = (
            '_data', '_idx', '_docsize', '_config', '_content',  # FTS5
            '_chunks', '_rowids', '_nodes', '_info'              # vec0
        )
        
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
                for suffix in shadow_suffixes:
                    if table == f"{base_table}{suffix}":
                        is_shadow = True
                        break
                if is_shadow:
                    break
            
            if is_shadow:
                log.dual_log(tag="DB:Validate", message=f"[{self.label}] Preserving shadow: {table}", payload={"label": self.label, "table": table, "action": "preserve_shadow"})
                continue
            
            if table not in expected:
                log.dual_log(tag="DB:Validate", level="WARNING", message=f"[{self.label}] Dropping unexpected table: {table}", payload={"label": self.label, "table": table, "action": "drop_unexpected"})
                self.conn.execute(f"DROP TABLE IF EXISTS {table}")
                report.add(ReconciliationAction(table, "pruned"))

    def _is_virtual_table(self, name: str) -> bool:
        """Check if a table is defined as VIRTUAL in sqlite_master."""
        res = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return res and "VIRTUAL" in (res[0] or "").upper()

    def _validate_structures(self, report: ReconciliationReport):
        """Deep validation of tables and columns with granular logging."""
        for name, ddl in self.expected_tables.items():
            log.dual_log(tag="DB:Validate", message=f"[{self.label}] Checking table: {name}", payload={"label": self.label, "table": name})
            
            exists = table_exists(self.conn, name)
            is_virtual = self._is_virtual_table(name)
            
            if not exists:
                log.dual_log(tag="DB:Validate", message=f"[{self.label}] Table {name} missing. Creating.", payload={"label": self.label, "table": name, "action": "create"})
                self.conn.executescript(ddl)
                report.add(ReconciliationAction(name, "created"))
                continue

            # Virtual Table Immunity: Skip deep column validation
            if is_virtual:
                log.dual_log(tag="DB:Validate", message=f"[{self.label}] {name}: Virtual table detected, skipping column check.", payload={"label": self.label, "table": name, "virtual": True})
                report.add(ReconciliationAction(name, "unchanged"))
                continue

            # Deep Column Check (Only for regular tables)
            actual_cols = {c.name.lower(): c for c in _get_columns(self.conn, name)}
            desired_cols = {c.name.lower(): c for c in (_columns_from_ddl_in_memory(ddl, name) or [])}
            
            missing = []
            type_mismatches = []
            constraint_mismatches = []
            
            for col_name, d_col in desired_cols.items():
                if col_name not in actual_cols:
                    missing.append(col_name)
                    log.dual_log(tag="DB:Validate", level="WARNING",
                               message=f"[{self.label}] {name}: Missing column '{col_name}'",
                               payload={"label": self.label, "table": name, "missing_column": col_name})
                else:
                    a_col = actual_cols[col_name]
                    # Use type affinity normalization
                    if _normalize_type_affinity(a_col.type) != _normalize_type_affinity(d_col.type):
                        type_mismatches.append((col_name, a_col.type, d_col.type))
                        log.dual_log(tag="DB:Validate", level="WARNING", message=f"[{self.label}] {name}: Type mismatch '{col_name}' (expected: {d_col.type}, actual: {a_col.type})", payload={"label": self.label, "table": name, "column": col_name, "expected": d_col.type, "actual": a_col.type})
                    if a_col.notnull != d_col.notnull:
                        constraint_mismatches.append((col_name, 'NOT NULL', d_col.notnull))
                    if a_col.pk != d_col.pk:
                        constraint_mismatches.append((col_name, 'PRIMARY KEY', d_col.pk))
            
            # Check for extra columns
            extra = set(actual_cols.keys()) - set(desired_cols.keys())
            for col_name in extra:
                log.dual_log(tag="DB:Validate", level="WARNING", message=f"[{self.label}] {name}: Extra column '{col_name}'", payload={"label": self.label, "table": name, "extra_column": col_name})
            
            has_drift = any([missing, type_mismatches, constraint_mismatches, extra])
            
            if has_drift:
                is_master = name in self.master_tables
                if is_master:
                    self._snapshot_master(name)
                
                reason_parts = []
                if missing: reason_parts.append(f"missing: {', '.join(missing)}")
                if type_mismatches: reason_parts.append(f"type_drift: {len(type_mismatches)}")
                if extra: reason_parts.append(f"extra: {', '.join(extra)}")
                reason = '; '.join(reason_parts)
                
                log.dual_log(tag="DB:Validate", level="WARNING", message=f"[{self.label}] Recreating {name} due to: {reason}", payload={"label": self.label, "table": name, "reason": reason})
                self.conn.execute(f"DROP TABLE IF EXISTS {name}")
                self.conn.executescript(ddl)
                report.add(ReconciliationAction(name, "recreated", is_master, reason))
            else:
                log.dual_log(tag="DB:Validate", message=f"[{self.label}] {name}: Structure valid", payload={"label": self.label, "table": name, "status": "valid"})
                report.add(ReconciliationAction(name, "unchanged"))

    def _validate_triggers(self, report: ReconciliationReport):
        """Validate triggers with logging."""
        for name, ddl in self.expected_triggers.items():
            exists = trigger_exists(self.conn, name)
            if not exists:
                log.dual_log(tag="DB:Validate", message=f"[{self.label}] Creating trigger: {name}", payload={"label": self.label, "trigger": name})
                self.conn.executescript(ddl)
                report.add(ReconciliationAction(name, "created"))
            else:
                log.dual_log(tag="DB:Validate", message=f"[{self.label}] Trigger {name} valid", payload={"label": self.label, "trigger": name, "status": "valid"})
                report.add(ReconciliationAction(name, "unchanged"))

    def _snapshot_master(self, table_name: str):
        """Pre-drop snapshot for master tables. If corrupted, log CRITICAL and proceed with reset."""
        try:
            from database.backup.exporter import export_table_chunks
            from database.backup.config import BackupConfig
            from database.backup.storage import write_table_batch
            
            cfg = BackupConfig.from_global_config()
            # This may raise OperationalError if table is corrupted
            chunks = export_table_chunks(self.conn, table_name, cfg, mode="full")
            written = write_table_batch(table_name, chunks, cfg)
            
            log.dual_log(tag="DB:Validate", level="INFO", message=f"[{self.label}] Pre-drop snapshot complete: {table_name} ({written} rows)", payload={"label": self.label, "table": table_name, "rows_written": written})
        except Exception as e:
            # FAIL-OPEN POLICY: Log CRITICAL, allow DROP/CREATE to proceed
            log.dual_log(
                tag="DB:Validate",
                level="CRITICAL",
                message=f"[{self.label}] {table_name} corrupted/unreadable. Snapshot failed. "
                        f"Proceeding with destructive reset to restore availability. Error: {e}",
                payload={"label": self.label, "table": table_name, "error": str(e)},
            )
            # Do not re-raise; allow the system to drop and recreate the broken table
