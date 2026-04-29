# database/management/__init__.py
"""Database Management System - Agnostic schema enforcement and lifecycle coordination."""

from database.management.reconciler import SchemaReconciler, ReconciliationAction, ReconciliationReport
from database.management.schema_introspector import (
    schema_matches, table_exists, trigger_exists,
    _get_columns, _columns_from_ddl_in_memory, _normalize_type_affinity
)
from database.management.lifecycle import run_database_lifecycle
from database.management.health import restore_orphaned_backup, check_database_file_state, check_tables_exist

__all__ = [
    # Reconciler
    "SchemaReconciler",
    "ReconciliationAction",
    "ReconciliationReport",
    
    # Schema Introspection
    "schema_matches",
    "table_exists",
    "trigger_exists",
    "_get_columns",
    "_columns_from_ddl_in_memory",
    "_normalize_type_affinity",
    
    # Lifecycle
    "run_database_lifecycle",
    
    # Health
    "restore_orphaned_backup",
    "check_database_file_state",
    "check_tables_exist",
]
