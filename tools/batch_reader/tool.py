# tools/batch_reader/tool.py
import json
import sqlite3
from typing import Any
from pydantic import BaseModel, Field

from tools.base import BaseTool
from database.connection import DatabaseManager
from utils.logger import get_dual_logger
from utils.callback_helper import format_callback_message
import config
from utils.hybrid_search import execute_hybrid_search

log = get_dual_logger(__name__)

class BatchReaderInput(BaseModel):
    batch_id: str = Field(..., description="The batch ID to query.")
    query: str = Field(..., description="Semantic search query.")
    limit: int = Field(5, description="Max results to return.")

class BatchReaderTool(BaseTool):
    name = "batch_reader"
    INPUT_MODEL = BatchReaderInput

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        def _fail(summary: str, next_steps: str) -> str:
            return json.dumps({
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": "FAILED",
                "summary": summary,
                "status_overrides": {
                    "FAILED": {
                        "description": "Batch Reader encountered a validation error.",
                        "next_steps": next_steps,
                        "rerunnable": False
                    }
                }
            }, ensure_ascii=False)

        batch_id = args.get("batch_id")
        query = args.get("query")
        limit = min(int(args.get("limit", 5)), 50)
        
        if not batch_id or not query:
            return _fail("batch_id and query are required.", "Provide both 'batch_id' and 'query' parameters.")
            
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        
        # 1. Get batch raw JSON path
        row = conn.execute("SELECT raw_json_path FROM broadcast_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if not row or not row["raw_json_path"]:
            return _fail("Batch not found or missing raw data.", "Verify the batch_id is valid. If lost, use the `scraper` tool to generate a new batch.")
            
        try:
            with open(row["raw_json_path"], "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception as e:
            return _fail(f"Failed to read batch data: {str(e)}", "Data may have been purged. Use the `scraper` tool to generate a new batch.")
            
        # 2. Extract valid ULIDs for this batch
        valid_ulids = []
        for item in raw_data.values() if isinstance(raw_data, dict) else raw_data:
            if isinstance(item, dict) and item.get("ulid"):
                valid_ulids.append(item["ulid"])
                
        if not valid_ulids:
            return _fail("No valid articles found in batch.", "The batch is empty or corrupted. Scrape a new batch.")

        # 3. Execute Hybrid Search
        w_vec = getattr(config, 'BATCH_READER_VECTOR_WEIGHT', 0.6)
        w_kw = getattr(config, 'BATCH_READER_KEYWORD_WEIGHT', 0.4)

        results = await execute_hybrid_search(
            query=query,
            valid_ulids=valid_ulids,
            limit=limit,
            w_vec=w_vec,
            w_kw=w_kw
        )
        
        # 4. Final Logging
        log.dual_log(
            tag="Search:Hybrid:Complete",
            message="Batch Reader delivered hybrid search results",
            payload={
                "batch_id": batch_id,
                "query": query,
                "returned_count": len(results),
                "limit_requested": limit
            }
        )
        
        status_overrides = {
            "COMPLETED": {
                "description": "Hybrid search (Vector + FTS5 RRF) completed successfully.",
                "next_steps": f"If the results do not answer your question, call `batch_reader` again with {{\"batch_id\": \"{batch_id}\", \"query\": \"<A DIFFERENT, REPHRASED QUERY>\"}}.",
                "rerunnable": True
            },
            "FAILED": {
                "description": "Batch Reader failed. Usually indicates invalid batch_id or corrupted data.",
                "next_steps": "Verify the batch_id is valid. If the error persists, use the `scraper` tool to generate a new batch.",
                "rerunnable": True
            }
        }
            
        payload = {
            "_callback_format": "structured",
            "tool_name": self.name,
            "status": "COMPLETED",
            "summary": f"Found {len(results)} relevant articles for query: '{query}'",
            "details": {
                "batch_id": batch_id,
                "query": query,
                "results": results
            },
            "status_overrides": status_overrides
        }
        return json.dumps(payload, ensure_ascii=False)
