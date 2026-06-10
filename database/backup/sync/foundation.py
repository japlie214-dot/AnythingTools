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

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
