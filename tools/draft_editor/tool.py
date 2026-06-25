# tools/draft_editor/tool.py
"""Draft Editor Tool - Deterministic SWAP list manager for Top 10 curation.

Performs purely programmatic list manipulations (internal index swap or
external ULID replacement) to ensure the Top 10 cardinality is strictly
maintained.

Activity-Driven Observability:
  This tool's run() method is decomposed into 6 named activities, each
  wrapped with the @activity decorator. When capture_lineage=true, the
  lineage report records each activity's inputs, outputs, and status.
  See utils/observability/activity_decorator.py.
"""

import json
import sqlite3
from typing import Any
from pydantic import BaseModel, Field

from tools.base import BaseTool, ToolExecutionError, ToolValidationError
from database.connection import DatabaseManager
from database.broadcast.queries import get_batch_info, get_batch_articles
from database.writer import enqueue_transaction
from utils.observability.activity_decorator import activity


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

    def is_resumable(self, args: dict[str, Any]) -> bool:
        return False

    # --- Activity-decomposed sub-methods ---
    # Each method raises on failure (never swallows). The @activity decorator
    # records PASSED on return, FAILED on raise, then re-raises.
    # Per Developer Contract in utils/observability/__init__.py §4.3.b: "No catch-all swallowing."

    @activity("Validate Batch ID")
    def _validate_batch_id(self, args: dict, job_id: str) -> str:
        """Extract and validate batch_id from args. Raises if missing."""
        batch_id = args.get("batch_id")
        if not batch_id:
            raise ToolExecutionError(
                "batch_id is required.",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Provide a valid 'batch_id' parameter.",
            )
        return batch_id

    @activity("Get Batch Info")
    def _get_batch_info(self, batch_id: str, job_id: str) -> dict:
        """Fetch batch info from DB. Raises if not found."""
        info = get_batch_info(batch_id)
        if not info:
            raise ToolExecutionError(
                "Batch not found.",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Verify the batch_id is valid.",
            )
        return info

    @activity("Validate Batch Status")
    def _validate_batch_status(self, batch_info: dict, job_id: str) -> dict:
        """Validate batch is in a swappable state. Raises if locked."""
        if batch_info["status"] not in ("PENDING", "PARTIAL", "FAILED"):
            raise ToolExecutionError(
                f"CRITICAL LOCK: Cannot execute SWAP. Status is '{batch_info['status']}'.",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Draft Editor modifications are locked to PENDING, PARTIAL, or FAILED batches to prevent state corruption."
            )
        return batch_info

    @activity("Apply Swap Operations")
    def _apply_swaps(self, top_10: list, inventory_dict: dict, operations: list) -> list:
        """Apply swap operations in-place on top_10. Returns mutated top_10."""
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
        return top_10

    @activity("Persist Updates")
    def _persist_updates(self, statements: list, batch_id: str, job_id: str) -> None:
        """Write updates to DB via enqueue_transaction. Raises on DB error."""
        try:
            enqueue_transaction(statements)
        except Exception as e:
            raise ToolExecutionError(
                f"Save failed: {e}",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Database write error. Check system logs.",
            )

    @activity("Build Summary")
    def _build_summary(self, batch_id: str, top_10: list, operations: list) -> str:
        """Build the markdown summary string."""
        summary_lines = [f"Successfully applied {len(operations)} SWAP operation(s) on batch {batch_id}.\n\n### New Top 10:"]
        for i, art in enumerate(top_10, 1):
            ulid = art.get("ulid", "unknown")
            title = art.get("title", "Untitled")
            if len(title) > 120: title = title[:117] + "..."
            summary_lines.append(f"**{i}.** [{ulid}] {title}")
        return "\n".join(summary_lines)

    # --- Entry point ---

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        """Execute the draft editor pipeline. Each step is an @activity."""
        job_id = kwargs.get("job_id")

        # Step 1: Validate batch_id (raises if missing).
        batch_id = self._validate_batch_id(args, job_id)

        # Step 2: Get batch info (raises if not found).
        batch_info = self._get_batch_info(batch_id, job_id)

        # Step 3: Validate batch status (raises if locked).
        self._validate_batch_status(batch_info, job_id)

        operations = args.get("operations", [])
        articles = get_batch_articles(batch_id)

        top_10 = [a for a in articles if a.get("is_top10")]
        top_10.sort(key=lambda x: (x.get("top10_rank") if x.get("top10_rank") is not None else 999, x.get("detail_id")))
        inventory_dict = {a["ulid"]: a for a in articles if not a.get("is_top10")}

        # Step 4: Apply swap operations.
        self._apply_swaps(top_10, inventory_dict, operations)

        # Build DB statements.
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

        # Step 5: Persist updates (raises on DB error).
        self._persist_updates(statements, batch_id, job_id)

        # Step 6: Build summary.
        summary = self._build_summary(batch_id, top_10, operations)

        # Append guidance section — preserves old status_overrides.COMPLETED.next_steps.
        guidance = (
            f"\n---\n### Status Guidance\n"
            f"**State: COMPLETED** — The Top 10 list has been successfully reordered/swapped.\n\n"
            f"**Next Steps:** You can now publish this edited batch using the `publisher` tool."
        )
        return summary + guidance
