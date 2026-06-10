# database/broadcast/queries.py
"""Read-only query helpers for broadcast_batches and broadcast_details.

All queries use DatabaseManager.get_read_connection() and respect
the query_only pragma.
"""

import sqlite3
from typing import List, Dict, Any, Optional

from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


def _get_cursor() -> sqlite3.Cursor:
    conn = DatabaseManager.get_read_connection()
    conn.row_factory = sqlite3.Row
    return conn.cursor()


def get_batch_info(batch_id: str) -> Optional[Dict[str, Any]]:
    cur = _get_cursor()
    cur.execute(
        "SELECT batch_id, target_site, article_count, top10_count, status, "
        "source_job_id, created_at, updated_at "
        "FROM broadcast_batches WHERE batch_id = ?",
        (batch_id,)
    )
    row = cur.fetchone()
    return dict(row) if row else None


def get_batch_article_ids(batch_id: str, status_filter: Optional[str] = None) -> List[str]:
    cur = _get_cursor()
    if status_filter:
        cur.execute(
            "SELECT bd.article_id FROM broadcast_details bd "
            "JOIN scraped_articles sa ON bd.article_id = sa.id "
            "WHERE bd.batch_id = ? AND bd.publish_status = ? "
            "AND sa.title IS NOT NULL AND sa.title != ''",
            (batch_id, status_filter)
        )
    else:
        cur.execute(
            "SELECT bd.article_id FROM broadcast_details bd "
            "JOIN scraped_articles sa ON bd.article_id = sa.id "
            "WHERE bd.batch_id = ? "
            "AND sa.title IS NOT NULL AND sa.title != ''",
            (batch_id,)
        )
    return [row["article_id"] for row in cur.fetchall()]


def get_batch_articles(batch_id: str, top10_only: bool = False) -> List[Dict[str, Any]]:
    """Return full article data for a batch, preserving explicit rank ordering."""
    cur = _get_cursor()
    sql = """
        SELECT
            sa.id as ulid, sa.url, sa.title, sa.conclusion, sa.summary,
            bd.is_top10, bd.top10_rank, bd.publish_status, bd.detail_id,
            bd.translated_title, bd.translated_summary, bd.translated_conclusion
        FROM broadcast_details bd
        JOIN scraped_articles sa ON bd.article_id = sa.id
        WHERE bd.batch_id = ?
    """
    params: list = [batch_id]

    if top10_only:
        sql += " AND bd.is_top10 = 1"

    sql += " ORDER BY bd.is_top10 DESC, bd.top10_rank ASC, bd.detail_id ASC"

    cur.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]


def get_batch_publish_progress(batch_id: str) -> Dict[str, int]:
    cur = _get_cursor()
    cur.execute(
        "SELECT publish_status, COUNT(*) as cnt "
        "FROM broadcast_details WHERE batch_id = ? "
        "GROUP BY publish_status",
        (batch_id,)
    )
    progress = {}
    for row in cur.fetchall():
        progress[row["publish_status"]] = row["cnt"]
    return progress


def get_details_for_publish(batch_id: str) -> List[Dict[str, Any]]:
    cur = _get_cursor()
    cur.execute(
        "SELECT sa.id as ulid, sa.url, sa.title, sa.conclusion, sa.summary, "
        "bd.is_top10, bd.top10_rank, bd.publish_status, bd.detail_id, "
        "bd.translated_title, bd.translated_summary, bd.translated_conclusion "
        "FROM broadcast_details bd "
        "JOIN scraped_articles sa ON bd.article_id = sa.id "
        "WHERE bd.batch_id = ? "
        "AND bd.publish_status IN ('PENDING', 'FAILED', 'TRANSLATING', 'PUBLISHED_BRIEFING') "
        "ORDER BY bd.is_top10 DESC, bd.top10_rank ASC, bd.detail_id ASC",
        (batch_id,)
    )
    return [dict(row) for row in cur.fetchall()]
