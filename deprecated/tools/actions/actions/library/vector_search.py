# deprecated/tools/actions/library/vector_search.py
"""Vector Search agent action for Librarian sub-agent.

Searches the `scraped_articles_vec` virtual table against an embedding of the
query and returns the top-k most relevant articles with similarity scores.
This is an internal action intended only for use by the Librarian.
"""

import json
from typing import Any

from tools.base import BaseTool
from database.connection import DatabaseManager
from utils.vector_search import generate_embedding


class VectorSearchAction(BaseTool):
    """Internal agent action: similarity search against KB."""

    from bot.core.constants import TOOL_LIBRARY_VECTOR_SEARCH
    name = TOOL_LIBRARY_VECTOR_SEARCH

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        query = args.get("query", "")
        limit = args.get("limit", 5)
        threshold = args.get("threshold", 0.50)

        if not query:
            return json.dumps({"count": 0, "data": [], "error": "Query is required"})

        try:
            query_embedding = await generate_embedding(query)

            conn = DatabaseManager.get_read_connection()
            sql = """
                SELECT a.title, a.conclusion, a.summary, (1 - v.distance) AS sim
                FROM scraped_articles_vec v
                JOIN scraped_articles a ON v.rowid = a.vec_rowid
                WHERE v.embedding MATCH ? AND k = ?
            """
            rows = conn.execute(sql, (query_embedding, limit)).fetchall()

            valid_results = [
                {"title": r["title"], "conclusion": r["conclusion"], "summary": r["summary"], "similarity": round(r["sim"], 3)}
                for r in rows if r["sim"] >= threshold
            ]

            return json.dumps({"count": len(valid_results), "data": valid_results})
        except Exception as e:
            return json.dumps({"count": 0, "data": [], "error": str(e)})
