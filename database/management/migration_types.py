# database/management/migration_types.py
from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum

class MigrationPhase(str, Enum):
    CLONE = "clone"
    RECREATE = "recreate"
    REPOPULATE = "repopulate"
    AUTOFILL = "autofill"
    VALIDATE = "validate"
    CLOUD_RECREATE = "cloud_recreate"
    CLOUD_REPOPULATE = "cloud_repopulate"
    CLOUD_VALIDATE = "cloud_validate"

class MigrationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"

@dataclass(frozen=True)
class ColumnMismatch:
    """A single column where actual type differs from expected."""
    column_name: str
    actual_type: str
    expected_type: str
    is_primary_key: bool

@dataclass
class TypeMismatchPlan:
    """Complete migration plan for a table with type mismatches."""
    table_name: str
    mismatches: List[ColumnMismatch]
    clone_table_name: str
    new_ddl: str
    columns_to_skip: List[str]
    pk_column: str
    total_rows: int = 0
    is_master: bool = False

@dataclass
class MigrationRecord:
    """Tracks the state of a single table migration."""
    table_name: str
    phase: MigrationPhase
    status: MigrationStatus = MigrationStatus.PENDING
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    error_message: Optional[str] = None
    rows_affected: int = 0
    clone_table_name: Optional[str] = None
