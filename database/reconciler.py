# file: database/reconciler.py

import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from database.schema_introspector import schema_matches, table_exists, trigger_exists, _get_columns, _columns_from_ddl_in_memory
from database.schemas import ALL_TABLES, ALL_VEC_TABLES, ALL_TRIGGERS, MASTER_TABLES
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

@dataclass(frozen=True)
class ReconciliationAction:
    table_name: str
    action: str  # created | unchanged | altered | recreated
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
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def reconcile(self) -> ReconciliationReport:
        report = ReconciliationReport()
        self.conn.execute("PRAGMA foreign_keys = OFF")

        try:
            direct_drift: Set[str] = set()

            # 1. Regular tables
            for name, ddl in ALL_TABLES.items():
                is_master = name in MASTER_TABLES
                if not table_exists(self.conn, name):
                    self.conn.executescript(ddl)
                    report.add(ReconciliationAction(name, "created", is_master))
                elif schema_matches(self.conn, name, ddl, is_virtual=False):
                    report.add(ReconciliationAction(name, "unchanged", is_master))
                else:
                    # Check ALTER TABLE shortcut
                    actual_cols = _get_columns(self.conn, name)
                    desired_cols = _columns_from_ddl_in_memory(ddl, name)
                    if desired_cols:
                        actual_names = {c.name.lower() for c in actual_cols}
                        missing_in_actual = [c for c in desired_cols if c.name.lower() not in actual_names]
                        
                        # Verify all existing columns match perfectly (no type or constraint drift)
                        all_existing_match = True
                        from database.schema_introspector import _normalize_type_affinity
                        actual_map = {c.name.lower(): c for c in actual_cols}
                        for d_col in desired_cols:
                            if d_col.name.lower() in actual_map:
                                a_col = actual_map[d_col.name.lower()]
                                if _normalize_type_affinity(a_col.type) != _normalize_type_affinity(d_col.type) or \
                                   a_col.notnull != d_col.notnull or a_col.pk != d_col.pk:
                                    all_existing_match = False
                                    break
                                    
                        if all_existing_match and missing_in_actual:
                            try:
                                for col in missing_in_actual:
                                    col_def = f"{col.name} {col.type}"
                                    if col.notnull:
                                        col_def += " NOT NULL"
                                    if col.dflt_value is not None:
                                        col_def += f" DEFAULT {col.dflt_value}"
                                    self.conn.execute(f"ALTER TABLE {name} ADD COLUMN {col_def}")
                                report.add(ReconciliationAction(name, "altered", is_master, reason="Additive column drift"))
                                continue
                            except sqlite3.OperationalError as e:
                                log.dual_log(tag="DB:Reconciler", level="WARNING", message=f"ALTER TABLE failed for {name}, falling back to recreate: {e}")
                    
                    direct_drift.add(name)
                    report.add(ReconciliationAction(name, "recreated", is_master, reason="Schema drift detected"))

            # 2. Virtual tables (vec0, FTS5)
            for name, ddl in ALL_VEC_TABLES.items():
                is_master = name in MASTER_TABLES
                if not table_exists(self.conn, name):
                    self.conn.executescript(ddl)
                    report.add(ReconciliationAction(name, "created", is_master))
                elif schema_matches(self.conn, name, ddl, is_virtual=True):
                    report.add(ReconciliationAction(name, "unchanged", is_master))
                else:
                    direct_drift.add(name)
                    report.add(ReconciliationAction(name, "recreated", is_master, reason="Virtual table schema drift"))

            # 3. Cascade recreation to children that FK to drifted parents
            tables_to_recreate = self._cascade_children(direct_drift)

            # 4. Perform drops and creates
            for name in sorted(tables_to_recreate):
                is_master = name in MASTER_TABLES
                reason = "Schema drift detected" if name in direct_drift else "Cascaded FK parent recreation"
                
                if name not in direct_drift:
                    report.add(ReconciliationAction(name, "recreated", is_master, reason=reason))

                if is_master:
                    # Pre-Drop Snapshot - FIXED: Use streaming export
                    try:
                        from tools.backup.exporter import export_table_chunks
                        from tools.backup.config import BackupConfig
                        from tools.backup.storage import write_table_batch
                        config = BackupConfig.from_global_config()
                        chunks = export_table_chunks(self.conn, name, config, mode="full")
                        written = write_table_batch(name, chunks, config)
                        if written > 0:
                            log.dual_log(tag="DB:Reconciler", level="INFO", message=f"Pre-Drop Snapshot succeeded for {name}")
                    except Exception as e:
                        # Halt entire process
                        log.dual_log(tag="DB:Reconciler", level="CRITICAL", message=f"Pre-Drop Snapshot failed for {name}. Halting to protect data: {e}", exc_info=e)
                        raise RuntimeError(f"Pre-Drop Snapshot failed for {name}. Halting to protect data: {e}")

                log.dual_log(tag="DB:Reconciler", level="WARNING", message=f"Dropping and recreating table: {name}")
                self.conn.execute(f"DROP TABLE IF EXISTS {name}")

                if name in ALL_TABLES:
                    self.conn.executescript(ALL_TABLES[name])
                elif name in ALL_VEC_TABLES:
                    self.conn.executescript(ALL_VEC_TABLES[name])

            # 5. Ensure triggers
            for name, ddl in ALL_TRIGGERS.items():
                if not trigger_exists(self.conn, name):
                    self.conn.executescript(ddl)
                    log.dual_log(tag="DB:Reconciler", level="INFO", message=f"Created trigger: {name}")

            self.conn.commit()
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

        return report

    def _cascade_children(self, direct_drift: Set[str]) -> Set[str]:
        result: Set[str] = set(direct_drift)
        parent_to_children: Dict[str, List[str]] = {}

        for row in self.conn.execute(
            """
            SELECT m.name as child_table, p."table" as parent_table
            FROM sqlite_master m
            JOIN pragma_foreign_key_list(m.name) p
            WHERE m.type = 'table'
            """
        ).fetchall():
            parent_to_children.setdefault(row[1], []).append(row[0])

        queue = list(direct_drift)
        while queue:
            parent = queue.pop(0)
            for child in parent_to_children.get(parent, []):
                if child not in result:
                    result.add(child)
                    queue.append(child)

        return result
