# database/backup/sync/content_hasher.py
import hashlib
from typing import List, Dict, Any
from database.backup.schema_registry import BackupSchemaRegistry

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
        all_cols = BackupSchemaRegistry.get_checksum_columns(table_name)
        return [c for c in all_cols if c.lower() not in cls.EXCLUDE_COLUMNS]

    @classmethod
    def compute_backfill_sql(cls, table_name: str) -> str:
        checksum_cols = cls.get_checksum_columns(table_name)
        if not checksum_cols:
            return ""
        concat_expr = " || ".join(f"COALESCE(quote({c}), '')" for c in checksum_cols)
        return (
            f"UPDATE {table_name} SET content_hash = lower(hex(sha256({concat_expr}))) "
            f"WHERE content_hash = '' OR content_hash IS NULL"
        )
