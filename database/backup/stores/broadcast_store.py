# database/backup/stores/broadcast_store.py
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from database.backup.base_store import JsonStore

class BroadcastBatchStore(JsonStore):
    entity_key = "batch_id"
    manifest_entity_key = "broadcast_batches"

    def build_upsert_statements(self, entity_id: str, data: dict, embedding_bytes: Optional[bytes] = None) -> List[Tuple[str, tuple]]:
        sql = """
            INSERT INTO broadcast_batches (batch_id, target_site, article_count, top10_count, status, source_job_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(batch_id) DO UPDATE SET
                target_site = excluded.target_site,
                article_count = excluded.article_count,
                top10_count = excluded.top10_count,
                status = excluded.status,
                source_job_id = excluded.source_job_id,
                updated_at = excluded.updated_at
        """
        return [(sql, (
            entity_id, data.get("target_site", ""), data.get("article_count", 0),
            data.get("top10_count", 0), data.get("status", "PENDING"),
            data.get("source_job_id"), data.get("created_at"), data.get("updated_at")
        ))]

    def build_delete_statements(self, entity_id: str) -> List[Tuple[str, tuple]]:
        return [("DELETE FROM broadcast_batches WHERE batch_id = ?", (entity_id,))]

    def get_all_from_sqlite(self, conn) -> List[dict]:
        return [dict(r) for r in conn.execute("SELECT * FROM broadcast_batches").fetchall()]

class BroadcastDetailStore(JsonStore):
    entity_key = "detail_id"
    manifest_entity_key = "broadcast_details"

    def build_upsert_statements(self, entity_id: str, data: dict, embedding_bytes: Optional[bytes] = None) -> List[Tuple[str, tuple]]:
        sql = """
            INSERT INTO broadcast_details (detail_id, batch_id, article_id, is_top10, top10_rank, publish_status, translated_title, translated_summary, translated_conclusion, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(detail_id) DO UPDATE SET
                is_top10 = excluded.is_top10,
                top10_rank = excluded.top10_rank,
                publish_status = excluded.publish_status,
                translated_title = excluded.translated_title,
                translated_summary = excluded.translated_summary,
                translated_conclusion = excluded.translated_conclusion,
                updated_at = excluded.updated_at
        """
        return [(sql, (
            entity_id, data.get("batch_id"), data.get("article_id"),
            data.get("is_top10", 0), data.get("top10_rank"), data.get("publish_status", "PENDING"),
            data.get("translated_title"), data.get("translated_summary"), data.get("translated_conclusion"),
            data.get("created_at"), data.get("updated_at")
        ))]

    def build_delete_statements(self, entity_id: str) -> List[Tuple[str, tuple]]:
        return [("DELETE FROM broadcast_details WHERE detail_id = ?", (entity_id,))]

    def get_all_from_sqlite(self, conn) -> List[dict]:
        return [dict(r) for r in conn.execute("SELECT * FROM broadcast_details").fetchall()]
