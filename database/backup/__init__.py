"""database/backup/__init__.py
Backup & Restore module for scraped articles.

Provides watermark-based delta export, immutable file storage,
and full restore capability with deduplication.

DEVELOPER NOTICE:
Dropping and recreating the database is the acceptable workflow for this environment.
Always use the established backup and hydrate mechanism for state transitions.
Do NOT write or add custom SQLite migration scripts.
"""

from database.backup.runner import BackupRunner
from database.backup.models import ExportResult, RestoreResult

__all__ = ["BackupRunner", "ExportResult", "RestoreResult"]
