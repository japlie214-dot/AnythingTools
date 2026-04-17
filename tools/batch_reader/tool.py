# tools/batch_reader/tool.py
import json
import sqlite3
from typing import Any
from pydantic import BaseModel, Field

from tools.base import BaseTool
from database.connection import DatabaseManager, SQLITE_VEC_AVAILABLE
from utils.vector_search import generate_embedding

class BatchReaderInput(BaseModel):
    batch_id: str = Field(..., description="The batch ID to query.")
    query: str = Field(..., description="Semantic search query.")
    limit: int = Field(5, description="Max results to return.")

class BatchReaderTool(BaseTool):
    name = "batch_reader"
    INPUT_MODEL = BatchReaderInput

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        if not SQLITE_VEC_AVAILABLE:
            return json.dumps({"error": "Vector search unavailable: sqlite_vec extension not loaded."})

        batch_id = args.get("batch_id")
        query = args.get("query")
        limit = min(int(args.get("limit", 5)), 50)
        
        if not batch_id or not query:
            return json.dumps({"error": "batch_id and query are required"})
            
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        
        # 1. Get batch raw JSON path
        row = conn.execute("SELECT raw_json_path FROM broadcast_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if not row or not row["raw_json_path"]:
            return json.dumps({"error": "Batch not found or missing raw data."})
            
        try:
            with open(row["raw_json_path"], "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception as e:
            return json.dumps({"error": f"Failed to read batch data: {str(e)}"})
            
        # 2. Extract valid ULIDs for this batch
        valid_ulids = []
        for item in raw_data.values() if isinstance(raw_data, dict) else raw_data:
            if isinstance(item, dict) and item.get("ulid"):
                valid_ulids.append(item["ulid"])
                
        if not valid_ulids:
            return json.dumps({"error": "No valid articles found in batch."})

        # 3. Vector search filtered by batch ULIDs
        query_embedding = await generate_embedding(query)
        pl = ",".join("?" for _ in valid_ulids)
        
        sql = f"""
            SELECT a.title, a.summary, a.conclusion, a.id as ulid, (1 - v.distance) AS sim
            FROM scraped_articles_vec v
            JOIN scraped_articles a ON v.rowid = a.vec_rowid
            WHERE v.embedding MATCH ? AND k = ?
            AND a.id IN ({pl})
        """
        
        rows = conn.execute(sql, [query_embedding, limit * 3] + valid_ulids).fetchall()
        
        results = []
        for r in rows[:limit]:
            results.append({
                "ulid": r["ulid"],
                "title": r["title"],
                "summary": r["summary"],
                "conclusion": r["conclusion"],
                "similarity": round(r["sim"], 3)
            })
            
        return json.dumps({"batch_id": batch_id, "query": query, "results": results}, ensure_ascii=False)
