"""database/backup/__init__.py
Backup & Restore module for scraped articles.

Provides watermark-based delta export, immutable file storage,
and full restore capability with deduplication.

DEVELOPER NOTICE:
Dropping and recreating the database is the acceptable workflow for this environment.
Always use the established backup and hydrate mechanism for state transitions.
Do NOT write or add custom SQLite migration scripts.
"""

from database.backup.settings import BackupSettings, LocalBackupSettings, CloudBackupSettings, SyncSettings
from database.backup.engine.local_engine import LocalEngine
from database.backup.engine.cloud_engine import CloudEngine
from database.backup.engine.dual_engine import DualEngine
from database.backup.models import ExportResult, RestoreResult, Watermark
from database.backup.observability.metrics import BackupMetricsCollector
from database.backup.resilience.circuit_breaker import CircuitBreaker, CircuitOpenError
from database.backup.sync.diff_engine import DiffEngine
from database.backup.sync.conflict_resolver import ConflictResolver
from database.backup.sync.ledger import SyncLedger
from database.backup.schema_registry import BackupSchemaRegistry
from database.backup.runner import BackupRunner

__all__ = [
    "BackupSettings", "LocalBackupSettings", "CloudBackupSettings", "SyncSettings",
    "LocalEngine", "CloudEngine", "DualEngine", "BackupRunner",
    "ExportResult", "RestoreResult", "Watermark",
    "BackupMetricsCollector", "CircuitBreaker", "CircuitOpenError",
    "DiffEngine", "ConflictResolver", "SyncLedger",
    "BackupSchemaRegistry"
]
