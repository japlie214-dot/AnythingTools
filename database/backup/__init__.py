"""database/backup/__init__.py
Backup & Restore module for scraped articles.

Provides watermark-based delta export, immutable file storage,
and full restore capability with deduplication.

DEVELOPER NOTICE:
Dropping and recreating the database is the acceptable workflow for this environment.
Always use the established backup and hydrate mechanism for state transitions.
Do NOT write or add custom SQLite migration scripts.
"""

from database.backup.settings import BackupSettings, CloudBackupSettings, SyncSettings
from database.backup.engine.cloud_engine import CloudEngine
from database.backup.engine.sync_engine import SyncEngine
from database.backup.models import ExportResult, RestoreResult, Watermark
from database.backup.sync.diff_engine import DiffEngine
from database.backup.sync.resolution import ConflictResolver, UserConfirmationHandler
from database.backup.sync.foundation import SyncLedger, ContentHasher
from database.backup.schema_registry import BackupSchemaRegistry
from database.backup.runner import BackupRunner

__all__ = [
    "BackupSettings", "CloudBackupSettings", "SyncSettings",
    "CloudEngine", "SyncEngine", "BackupRunner",
    "ExportResult", "RestoreResult", "Watermark",
    "DiffEngine", "ConflictResolver", "SyncLedger", "ContentHasher",
    "BackupSchemaRegistry"
]
