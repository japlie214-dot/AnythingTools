"""tools/draft_editor/tool.py

Draft Editor Tool - Deterministic SWAP list manager for Top 10 curation.

Performs purely programmatic list manipulations (internal index swap or external ULID replacement)
to ensure the Top 10 cardinality is strictly maintained.
"""

import json
import sqlite3
from typing import Any
from pydantic import BaseModel, Field

from tools.base import BaseTool, HealthCheckPayload, ToolExecutionError, ToolValidationError
from database.connection import DatabaseManager
from database.broadcast.queries import get_batch_info, get_batch_articles
from database.writer import enqueue_transaction


class SwapOperation(BaseModel):
    index_top10: int = Field(..., ge=0)
    target_identifier: str | int = Field(...)

class DraftEditorInput(BaseModel):
    batch_id: str = Field(..., description="The unique ULID of the batch to edit.")
    operations: list[SwapOperation] = Field([], description="List of SWAP operations: [{'index_top10': 0, 'target_identifier': 'ULID_OR_INT'}]")

INPUT_MODEL = DraftEditorInput


class DraftEditorTool(BaseTool):
    """Draft Editor Tool: Deterministic SWAP list manager for Top 10 curation."""
    
    name = "draft_editor"

    def health_check_payload(self) -> HealthCheckPayload:
        return HealthCheckPayload(
            happy_path_args={"batch_id": "HEALTH_CHECK_TEST_BATCH", "operations": []},
            error_path_args={"batch_id": "NONEXISTENT_BATCH_ID_12345", "operations": [{"index_top10": 0, "target_identifier": "invalid"}]},
            expected_happy_status="COMPLETED",
            expected_error_status="FAILED",
            timeout_seconds=30,
        )

    def is_resumable(self, args: dict[str, Any]) -> bool:
        return False

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        def _fail(summary: str, next_steps: str) -> None:
            raise ToolExecutionError(
                summary,
                tool_name=self.name,
                job_id=kwargs.get("job_id"),
                next_steps=next_steps,
            )

        batch_id = args.get("batch_id")
        operations = args.get("operations", [])
        
        if not batch_id:
            _fail("batch_id is required.", "Provide a valid 'batch_id' parameter.")

        batch_info = get_batch_info(batch_id)
        if not batch_info:
            _fail("Batch not found.", "Verify the batch_id is valid.")

        if batch_info["status"] not in ("PENDING", "PARTIAL", "FAILED"):
            _fail(
                f"CRITICAL LOCK: Cannot execute SWAP. Status is '{batch_info['status']}'.",
                "Draft Editor modifications are locked to PENDING, PARTIAL, or FAILED batches to prevent state corruption."
            )

        articles = get_batch_articles(batch_id)
        
        top_10 = [a for a in articles if a.get("is_top10")]
        top_10.sort(key=lambda x: (x.get("top10_rank") if x.get("top10_rank") is not None else 999, x.get("detail_id")))
        inventory_dict = {a["ulid"]: a for a in articles if not a.get("is_top10")}

        for op in operations:
            if hasattr(op, "model_dump"): op = op.model_dump()
            elif hasattr(op, "dict"): op = op.dict()
            idx = op.get("index_top10")
            target = op.get("target_identifier")
            
            if idx is None or target is None or idx < 0 or idx >= len(top_10):
                continue
                
            if isinstance(target, int) and 0 <= target < len(top_10):
                top_10[idx], top_10[target] = top_10[target], top_10[idx]
            elif isinstance(target, str):
                replacement = inventory_dict.get(target)
                if replacement:
                    old_item = top_10[idx]
                    top_10[idx] = replacement
                    inventory_dict[old_item["ulid"]] = old_item
                    del inventory_dict[target]

        statements = []
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        
        for new_rank, art in enumerate(top_10):
            statements.append((
                "UPDATE broadcast_details SET is_top10 = 1, top10_rank = ?, updated_at = ? WHERE batch_id = ? AND article_id = ?",
                (new_rank, ts, batch_id, art["ulid"])
            ))
            
        for art in inventory_dict.values():
            statements.append((
                "UPDATE broadcast_details SET is_top10 = 0, top10_rank = NULL, updated_at = ? WHERE batch_id = ? AND article_id = ?",
                (ts, batch_id, art["ulid"])
            ))

        try:
            enqueue_transaction(statements)
        except Exception as e:
            _fail(f"Save failed: {e}", "Database write error. Check system logs.")

        summary_lines = [f"Successfully applied {len(operations)} SWAP operation(s) on batch {batch_id}.\n\n### New Top 10:"]
        for i, art in enumerate(top_10, 1):
            ulid = art.get("ulid", "unknown")
            title = art.get("title", "Untitled")
            if len(title) > 120: title = title[:117] + "..."
            summary_lines.append(f"**{i}.** [{ulid}] {title}")

        payload = {
            "_callback_format": "structured",
            "tool_name": self.name,
            "status": "COMPLETED",
            "summary": "\n".join(summary_lines),
            "details": {"batch_id": batch_id, "new_top10_ulids": [a["ulid"] for a in top_10]},
            "status_overrides": {
                "COMPLETED": {
                    "description": "The Top 10 list has been successfully reordered/swapped.",
                    "next_steps": f"You can now publish this edited batch using the `publisher` tool.",
                    "rerunnable": True
                }
            }
        }
        return json.dumps(payload, ensure_ascii=False)
