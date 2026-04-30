"""utils/hybrid_search.py
Hybrid Search utilities: FTS5 sanitization, Weighted RRF, and orchestration.
"""
import re
import sqlite3
from typing import List, Dict, Any

from database.connection import DatabaseManager, SQLITE_VEC_AVAILABLE
from utils.vector_search import generate_embedding
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def sanitize_fts_query(query: str) -> str:
    """
    Remove FTS5 reserved characters to prevent SQLite syntax errors.
    Leaves only alphanumeric characters and spaces.
    """
    sanitized = re.sub(r'[^\w\s]', ' ', query)
    # Collapse multiple spaces and strip
    return re.sub(r'\s+', ' ', sanitized).strip()

def weighted_rrf(
    vector_results: List[Dict[str, Any]], 
    keyword_results: List[Dict[str, Any]], 
    w_vec: float, 
    w_kw: float, 
    k: int = 60
) -> List[Dict[str, Any]]:
    """
    Apply Weighted Reciprocal Rank Fusion (RRF) to two ranked lists.
    """
    scores: Dict[str, float] = {}
    items: Dict[str, Dict[str, Any]] = {}

    # Score vector results
    for rank, item in enumerate(vector_results, start=1):
        ulid = item['ulid']
        scores[ulid] = scores.get(ulid, 0.0) + (w_vec / (k + rank))
        items[ulid] = item

    # Score keyword results
    for rank, item in enumerate(keyword_results, start=1):
        ulid = item['ulid']
        scores[ulid] = scores.get(ulid, 0.0) + (w_kw / (k + rank))
        if ulid not in items:
            items[ulid] = item

    # Sort by fusion score descending
    sorted_ulids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)

    fused_results = []
    for ulid in sorted_ulids:
        res = items[ulid]
        res['fusion_score'] = round(scores[ulid], 5)
        fused_results.append(res)

    return fused_results

async def execute_hybrid_search(
    query: str, 
    valid_ulids: List[str], 
    limit: int, 
    w_vec: float, 
    w_kw: float
) -> List[Dict[str, Any]]:
    """
    Execute parallel Vector and FTS5 searches, then fuse results using RRF.
    """
    if not valid_ulids:
        return []

    conn = DatabaseManager.get_read_connection()
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in valid_ulids)

    # 1. Vector Search
    vec_results = []
    if SQLITE_VEC_AVAILABLE:
        try:
            query_embedding = await generate_embedding(query)
            vec_sql = f"""
                SELECT a.title, a.summary, a.conclusion, a.id as ulid, (1 - v.distance) AS sim
                FROM scraped_articles_vec v
                JOIN scraped_articles a ON v.rowid = a.vec_rowid
                WHERE v.embedding MATCH ? AND k = ?
                AND a.id IN ({placeholders})
                ORDER BY v.distance ASC
            """
            vec_rows = conn.execute(vec_sql, [query_embedding, limit * 3] + valid_ulids).fetchall()
            vec_results = [dict(r) for r in vec_rows]
        except Exception as e:
            log.dual_log(tag="Search:Hybrid:Vector", message=f"Vector search failed: {e}", level="WARNING", exc_info=e, payload={"query": query, "error": str(e)})

    # 2. Keyword Search (FTS5)
    kw_results = []
    safe_query = sanitize_fts_query(query)
    
    if safe_query:
        try:
            kw_sql = f"""
                SELECT a.title, a.summary, a.conclusion, a.id as ulid, f.rank as fts_rank
                FROM scraped_articles_fts f
                JOIN scraped_articles a ON a.vec_rowid = f.rowid
                WHERE scraped_articles_fts MATCH ?
                AND a.id IN ({placeholders})
                ORDER BY f.rank
                LIMIT ?
            """
            kw_rows = conn.execute(kw_sql, [safe_query] + valid_ulids + [limit * 3]).fetchall()
            kw_results = [dict(r) for r in kw_rows]
        except Exception as e:
            log.dual_log(tag="Search:Hybrid:Keyword", message=f"FTS5 keyword search failed: {e}", level="WARNING", exc_info=e, payload={"safe_query": safe_query, "error": str(e)})

    # 3. Telemetry Logging
    log.dual_log(
        tag="Search:Hybrid:Execute",
        message="Hybrid search components executed",
        payload={
            "raw_query": query,
            "safe_query": safe_query,
            "vector_count": len(vec_results),
            "keyword_count": len(kw_results),
            "weights": {"vector": w_vec, "keyword": w_kw}
        }
    )

    # 4. Reciprocal Rank Fusion
    fused = weighted_rrf(vec_results, kw_results, w_vec, w_kw)
    return fused[:limit]
