# database/management/__init__.py
"""Database Management System — Schema enforcement, lifecycle coordination, and health monitoring.

This package provides the core infrastructure for keeping the application's
SQLite databases in sync with their canonical schema definitions. It is
designed to be **agnostic** — no domain-specific imports or hardcoded table
names exist within the management layer. All schema knowledge is injected
via function parameters.

Architecture
============
The system is composed of four cooperating modules:

1. **SchemaReconciler** (`reconciler.py`)
   The central engine that compares the actual runtime schema against the
   desired canonical DDL. For each table, it performs:
   - Existence check (missing tables are created)
   - Deep column validation (type affinity, NOT NULL, PRIMARY KEY)
   - Virtual table immunity (FTS5 and vec0 tables skip column checks)
   - Unexpected table pruning (drops tables not in the expected schema,
     preserving FTS5/vec0 shadow tables like `_chunks`, `_rowids`,
     `_vector_chunks00`, `_data`, `_idx`, `_docsize`, `_config`)
   - Trigger validation (creates missing triggers)
   Returns a `ReconciliationReport` listing every action taken.

2. **SchemaIntrospector** (`schema_introspector.py`)
   Low-level schema inspection utilities:
   - `schema_matches()`: Deep comparison of a table's columns against DDL
   - `table_exists()` / `trigger_exists()`: Existence checks via sqlite_master
   - `_get_columns()`: Column metadata via `PRAGMA table_info`
   - `_columns_from_ddl_in_memory()`: Executes DDL on an in-memory database
     to extract column metadata without affecting the production DB
   - `_normalize_type_affinity()`: Maps SQLite type strings to their 5
     canonical affinities (INTEGER, TEXT, BLOB, REAL, NUMERIC)

3. **Lifecycle** (`lifecycle.py`)
   Orchestrator that runs the full validation sequence across all registered
   database contexts (currently: Operational DB + Logs DB). For each context:
   - Restores orphaned backups (.db.bak → .db)
   - Checks for corruption (SELECT 1 FROM sqlite_master)
   - Runs SchemaReconciler against expected schemas
   - Performs WAL checkpoint after successful reconciliation
   On fresh databases, it initializes schema via the writer queue.

4. **Health** (`health.py`)
   Backup recovery and corruption detection:
   - `restore_orphaned_backup()`: If a .db.bak file exists, copies it over
     the .db file and removes the backup
   - `check_database_file_state()`: Returns (exists, is_corrupted)
   - `check_tables_exist()`: Verifies all expected tables are present
"""

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
