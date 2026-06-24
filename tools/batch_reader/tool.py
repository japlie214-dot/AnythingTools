# tools/batch_reader/tool.py
"""Batch Reader Tool - Hybrid semantic search across a batch's articles.

Returns plain markdown with search results and query-iteration guidance.

Activity-Driven Observability:
  Decomposed into 4 named activities. See utils/observability/activity_decorator.py.
"""

import json
import sqlite3
from typing import Any
from pydantic import BaseModel, Field

from tools.base import BaseTool, ToolExecutionError, ToolValidationError
from database.connection import DatabaseManager
from utils.logger import get_dual_logger
import config
from utils.hybrid_search import execute_hybrid_search
from utils.observability.activity_decorator import activity

log = get_dual_logger(__name__)

class BatchReaderInput(BaseModel):
    batch_id: str = Field(..., description="The batch ID to query.")
    query: str = Field(..., description="Semantic search query.")
    limit: int = Field(5, description="Max results to return.")

class BatchReaderTool(BaseTool):
    name = "batch_reader"
    INPUT_MODEL = BatchReaderInput

    # --- Activity-decomposed sub-methods ---

    @activity("Validate BatchReader Input")
    def _validate_batch_reader_input(self, args: dict, job_id: str) -> tuple:
        """Extract and validate batch_id, query, limit. Raises on missing fields."""
        batch_id = args.get("batch_id")
        query = args.get("query")
        limit = min(int(args.get("limit", 5)), 50)
        if not batch_id or not query:
            raise ToolExecutionError(
                "batch_id and query are required.",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Provide both 'batch_id' and 'query' parameters.",
            )
        return batch_id, query, limit

    @activity("Fetch Batch Article IDs")
    def _fetch_batch_article_ids(self, batch_id: str, job_id: str) -> list:
        """Fetch valid article ULIDs for the batch. Raises if batch not found or empty."""
        from database.broadcast.queries import get_batch_info, get_batch_article_ids

        batch_info = get_batch_info(batch_id)
        if not batch_info:
            raise ToolExecutionError(
                "Batch not found.",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Verify the batch_id is valid. If lost, use the `scraper` tool to generate a new batch.",
            )
        valid_ulids = get_batch_article_ids(batch_id)
        if not valid_ulids:
            raise ToolExecutionError(
                "No valid articles found in batch.",
                tool_name=self.name,
                job_id=job_id,
                next_steps="The batch is empty or corrupted. Scrape a new batch.",
            )
        return valid_ulids

    @activity("Execute Hybrid Search")
    async def _execute_hybrid_search(self, query: str, valid_ulids: list, limit: int) -> list:
        """Run the hybrid vector + FTS5 search. Returns ranked results."""
        w_vec = getattr(config, 'BATCH_READER_VECTOR_WEIGHT', 0.6)
        w_kw = getattr(config, 'BATCH_READER_KEYWORD_WEIGHT', 0.4)
        return await execute_hybrid_search(
            query=query,
            valid_ulids=valid_ulids,
            limit=limit,
            w_vec=w_vec,
            w_kw=w_kw,
        )

    @activity("Build Search Results Markdown")
    def _build_search_markdown(self, batch_id: str, query: str, results: list, limit: int) -> str:
        """Build the markdown summary with search results and query-iteration guidance.

        The guidance section preserves the old status_overrides.COMPLETED.next_steps
        behavior — telling the agent to rephrase the query if results are insufficient.
        """
        summary_parts = [f"Found {len(results)} relevant article(s) for query: '{query}' (batch: {batch_id})"]

        if results:
            summary_parts.append("\n### Search Results")
            for idx, article in enumerate(results, 1):
                ulid = article.get("ulid", article.get("id", "unknown"))
                title = article.get("title", "Untitled")
                if len(title) > 120:
                    title = title[:117] + "..."

                score = article.get("fusion_score")
                score_str = f" (score: {score})" if score is not None else ""
                summary_parts.append(f"\n**{idx}. [{ulid}] {title}**{score_str}")

                conclusion = article.get("conclusion", "")
                if conclusion:
                    if len(conclusion) > 5000:
                        conclusion = conclusion[:4997] + "..."
                    summary_parts.append(f"  Conclusion: {conclusion}")

                art_summary = article.get("summary", "")
                if art_summary:
                    if len(art_summary) > 9000:
                        art_summary = art_summary[:8997] + "..."
                    art_summary = art_summary.replace('\n', ' ')
                    summary_parts.append(f"  Summary: {art_summary}")
        else:
            summary_parts.append("\n_No articles matched the query. Try rephrasing or using different keywords._")

        # Query-iteration guidance — preserves old status_overrides behavior.
        # The agent uses this to decide whether to search again with different terms.
        summary_parts.append(
            f"\n---\n### Search Guidance\n"
            f"If the results do not answer your question, call `batch_reader` again with "
            f'{{"batch_id": "{batch_id}", "query": "<A DIFFERENT, REPHRASED QUERY>"}}.'
        )

        log.dual_log(
            tag="Search:Hybrid:Complete",
            message="Batch Reader delivered hybrid search results",
            payload={"batch_id": batch_id, "query": query, "returned_count": len(results), "limit_requested": limit},
        )

        return "\n".join(summary_parts)

    # --- Entry point ---

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        """Execute hybrid search and return markdown results with guidance."""
        job_id = kwargs.get("job_id")

        # Step 1: Validate input.
        batch_id, query, limit = self._validate_batch_reader_input(args, job_id)

        # Step 2: Fetch batch article IDs.
        valid_ulids = self._fetch_batch_article_ids(batch_id, job_id)

        # Step 3: Execute hybrid search.
        results = await self._execute_hybrid_search(query, valid_ulids, limit)

        # Step 4: Build markdown.
        return self._build_search_markdown(batch_id, query, results, limit)
