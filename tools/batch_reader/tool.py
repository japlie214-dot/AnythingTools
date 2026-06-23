# tools/batch_reader/tool.py
import json
import sqlite3
from typing import Any
from pydantic import BaseModel, Field

from tools.base import BaseTool, HealthCheckPayload, ToolExecutionError, ToolValidationError
from database.connection import DatabaseManager
from utils.logger import get_dual_logger
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

    def health_check_payload(self) -> HealthCheckPayload:
        return HealthCheckPayload(
            happy_path_args={"batch_id": "HEALTH_CHECK_TEST_BATCH", "query": "test", "limit": 3},
            error_path_args={"batch_id": "NONEXISTENT_BATCH_ID_12345", "query": "test"},
            expected_happy_status="COMPLETED",
            expected_error_status="FAILED",
            timeout_seconds=30,
        )

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        def _fail(summary: str, next_steps: str) -> None:
            raise ToolExecutionError(
                summary,
                tool_name=self.name,
                job_id=kwargs.get("job_id"),
                next_steps=next_steps,
            )

        batch_id = args.get("batch_id")
        query = args.get("query")
        limit = min(int(args.get("limit", 5)), 50)
        
        if not batch_id or not query:
            _fail("batch_id and query are required.", "Provide both 'batch_id' and 'query' parameters.")
            
        from database.broadcast.queries import get_batch_info, get_batch_article_ids
        
        batch_info = get_batch_info(batch_id)
        if not batch_info:
            _fail("Batch not found.", "Verify the batch_id is valid. If lost, use the `scraper` tool to generate a new batch.")
            
        valid_ulids = get_batch_article_ids(batch_id)
        if not valid_ulids:
            _fail("No valid articles found in batch.", "The batch is empty or corrupted. Scrape a new batch.")

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
            
        summary_parts = [f"Found {len(results)} relevant article(s) for query: '{query}' (batch: {batch_id})"]
        
        if results:
            summary_parts.append("\n### Search Results")
            for idx, article in enumerate(results, 1):
                ulid = article.get("ulid", article.get("id", "unknown"))
                title = article.get("title", "Untitled")
                if len(title) > 120: title = title[:117] + "..."
                
                score = article.get("fusion_score")
                score_str = f" (score: {score})" if score is not None else ""
                summary_parts.append(f"\n**{idx}. [{ulid}] {title}**{score_str}")
                
                conclusion = article.get("conclusion", "")
                if conclusion:
                    if len(conclusion) > 5000: conclusion = conclusion[:4997] + "..."
                    summary_parts.append(f"  Conclusion: {conclusion}")
                
                art_summary = article.get("summary", "")
                if art_summary:
                    if len(art_summary) > 9000: art_summary = art_summary[:8997] + "..."
                    art_summary = art_summary.replace('\n', ' ')
                    summary_parts.append(f"  Summary: {art_summary}")
        else:
            summary_parts.append("\n_No articles matched the query. Try rephrasing or using different keywords._")

        payload = {
            "_callback_format": "structured",
            "tool_name": self.name,
            "status": "COMPLETED",
            "summary": "\n".join(summary_parts),
            "details": {
                "batch_id": batch_id,
                "query": query,
                "results": results
            },
            "status_overrides": status_overrides
        }
        return json.dumps(payload, ensure_ascii=False)
