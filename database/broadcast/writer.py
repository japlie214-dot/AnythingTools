# database/broadcast/writer.py
"""Write operations for broadcast_batches and broadcast_details.

All writes go through database.writer.enqueue_write/enqueue_transaction
to maintain the single-writer-thread contract.
"""

import json
from datetime import datetime, timezone
from typing import List, Dict, Optional

from database.writer import enqueue_write, enqueue_transaction
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_broadcast_batch(
    batch_id: str,
    target_site: str,
    article_count: int = 0,
    top10_count: int = 0,
    source_job_id: Optional[str] = None,
) -> None:
    now = _utcnow()
    enqueue_write(
        "INSERT INTO broadcast_batches "
        "(batch_id, target_site, article_count, top10_count, status, source_job_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)",
        (batch_id, target_site, article_count, top10_count, source_job_id, now, now)
    )
    try:
        from database.backup.writer.cloud_writer import enqueue_cloud_write
        enqueue_cloud_write("broadcast_batches", {
            "batch_id": batch_id, "target_site": target_site, "article_count": article_count,
            "top10_count": top10_count, "status": "PENDING", "source_job_id": source_job_id,
            "created_at": now, "updated_at": now
        }, pk_col="batch_id")
    except Exception:
        pass


def add_broadcast_detail(
    batch_id: str,
    article_id: str,
    is_top10: bool = False,
    top10_rank: Optional[int] = None,
) -> None:
    enqueue_write(
        "INSERT OR IGNORE INTO broadcast_details "
        "(batch_id, article_id, is_top10, top10_rank, publish_status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'PENDING', ?, ?)",
        (batch_id, article_id, 1 if is_top10 else 0, top10_rank, _utcnow(), _utcnow())
    )


def add_broadcast_details_bulk(
    batch_id: str,
    articles: List[Dict],
    top10_list: List[Dict],
) -> None:
    """Bulk-insert broadcast_details rows, preserving exact top-10 ranking."""
    now = _utcnow()
    top10_ulids = {a.get("ulid"): rank for rank, a in enumerate(top10_list)}
    statements = []
    
    for article in articles:
        article_id = article.get("ulid") or article.get("id")
        if not article_id:
            continue
            
        rank = top10_ulids.get(article_id)
        is_top10 = 1 if rank is not None else 0
        
        statements.append((
            "INSERT OR IGNORE INTO broadcast_details "
            "(batch_id, article_id, is_top10, top10_rank, publish_status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'PENDING', ?, ?)",
            (batch_id, article_id, is_top10, rank, now, now)
        ))
    if statements:
        enqueue_transaction(statements)


def update_detail_publish_status(
    batch_id: str,
    article_id: str,
    publish_status: str,
    translated_title: Optional[str] = None,
    translated_summary: Optional[str] = None,
    translated_conclusion: Optional[str] = None,
) -> None:
    if translated_title is not None:
        enqueue_write(
            "UPDATE broadcast_details SET publish_status = ?, "
            "translated_title = ?, translated_summary = ?, translated_conclusion = ?, "
            "updated_at = ? "
            "WHERE batch_id = ? AND article_id = ?",
            (publish_status, translated_title, translated_summary, translated_conclusion,
             _utcnow(), batch_id, article_id)
        )
    else:
        enqueue_write(
            "UPDATE broadcast_details SET publish_status = ?, updated_at = ? "
            "WHERE batch_id = ? AND article_id = ?",
            (publish_status, _utcnow(), batch_id, article_id)
        )
    try:
        from database.connection import DatabaseManager
        from database.backup.writer.cloud_writer import enqueue_cloud_write
        conn = DatabaseManager.get_read_connection()
        row = conn.execute("SELECT * FROM broadcast_details WHERE batch_id = ? AND article_id = ?", (batch_id, article_id)).fetchone()
        if row:
            enqueue_cloud_write("broadcast_details", dict(row), pk_col="detail_id")
    except Exception:
        pass


def mark_detail_published(
    batch_id: str,
    article_id: str,
    phase: str,
) -> None:
    status_map = {
        "briefing": "PUBLISHED_BRIEFING",
        "archive": "PUBLISHED_ARCHIVE",
    }
    new_status = status_map.get(phase)
    if not new_status:
        log.dual_log(
            tag="Broadcast:Detail:InvalidPhase",
            message=f"Invalid publish phase: {phase}",
            level="ERROR",
            payload={"batch_id": batch_id, "article_id": article_id, "phase": phase}
        )
        return
    enqueue_write(
        "UPDATE broadcast_details SET publish_status = ?, updated_at = ? "
        "WHERE batch_id = ? AND article_id = ?",
        (new_status, _utcnow(), batch_id, article_id)
    )
    try:
        from database.connection import DatabaseManager
        from database.backup.writer.cloud_writer import enqueue_cloud_write
        conn = DatabaseManager.get_read_connection()
        row = conn.execute("SELECT * FROM broadcast_details WHERE batch_id = ? AND article_id = ?", (batch_id, article_id)).fetchone()
        if row:
            enqueue_cloud_write("broadcast_details", dict(row), pk_col="detail_id")
    except Exception:
        pass


def update_batch_status_from_details(batch_id: str) -> None:
    from database.connection import DatabaseManager
    conn = DatabaseManager.get_read_connection()
    row = conn.execute(
        "SELECT "
        "  COUNT(*) as total, "
        "  SUM(CASE WHEN publish_status = 'PUBLISHED_ARCHIVE' THEN 1 ELSE 0 END) as published, "
        "  SUM(CASE WHEN publish_status = 'FAILED' THEN 1 ELSE 0 END) as failed, "
        "  SUM(CASE WHEN publish_status = 'SKIPPED' THEN 1 ELSE 0 END) as skipped "
        "FROM broadcast_details WHERE batch_id = ?",
        (batch_id,)
    ).fetchone()

    if not row or row["total"] == 0:
        return

    total = row["total"]
    published = row["published"] or 0
    failed = row["failed"] or 0
    skipped = row["skipped"] or 0
    pending = total - (published + failed + skipped)

    if (published + skipped) == total and published > 0:
        new_status = "COMPLETED"
    elif failed == total or (failed + skipped) == total:
        new_status = "FAILED"
    elif published > 0 or failed > 0:
        new_status = "PARTIAL"
    else:
        new_status = "PENDING"

    enqueue_write(
        "UPDATE broadcast_batches SET status = ?, updated_at = ? WHERE batch_id = ?",
        (new_status, _utcnow(), batch_id)
    )
    try:
        from database.backup.writer.cloud_writer import enqueue_cloud_write
        batch_row = conn.execute("SELECT * FROM broadcast_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if batch_row:
            enqueue_cloud_write("broadcast_batches", dict(batch_row), pk_col="batch_id")
    except Exception:
        pass


def reset_batch_publish_status(batch_id: str) -> None:
    enqueue_write(
        "UPDATE broadcast_details SET publish_status = 'PENDING', "
        "translated_title = NULL, translated_summary = NULL, translated_conclusion = NULL, "
        "updated_at = ? "
        "WHERE batch_id = ?",
        (_utcnow(), batch_id)
    )
