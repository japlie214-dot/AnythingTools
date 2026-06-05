# database/backup/sync/foundation.py
import hashlib
from typing import List, Dict, Any
from datetime import datetime, timezone

class ContentHasher:
    EXCLUDE_COLUMNS = frozenset({"updated_at", "content_hash", "scraped_at", "embedding", "vec_rowid", "created_at"})

    @classmethod
    def compute_row_hash(cls, table_name: str, row: Dict[str, Any]) -> str:
        checksum_cols = cls.get_checksum_columns(table_name)
        parts = []
        for col in checksum_cols:
            val = row.get(col)
            normalized = str(val or "").strip()
            parts.append(normalized)
        concat = "||".join(parts)
        return hashlib.sha256(concat.encode("utf-8")).hexdigest()

    @classmethod
    def get_checksum_columns(cls, table_name: str) -> List[str]:
        # Best-effort: if schema registry is available, use it; otherwise fallback to all keys on row usage.
        try:
            from database.backup.schema_registry import BackupSchemaRegistry
            all_cols = BackupSchemaRegistry.get_checksum_columns(table_name)
            return [c for c in all_cols if c.lower() not in cls.EXCLUDE_COLUMNS]
        except Exception:
            return []

class SyncLedger:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS sync_ledger (
        operation_id TEXT PRIMARY KEY,
        table_name TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('LOCAL_TO_CLOUD', 'CLOUD_TO_LOCAL', 'BIDIRECTIONAL')),
        row_count INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
        started_at TEXT,
        completed_at TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_sync_ledger_state ON sync_ledger(state, table_name);
    """

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
