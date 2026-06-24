# tools/publisher/tool.py
"""Publisher Tool - Translates and delivers curated intelligence via Producer-Consumer pipeline.

Returns plain markdown. The markdown contains an explicit status section that
preserves the semantic guidance (PARTIAL resumption instructions, COMPLETED
confirmation, FAILED diagnostics) that the LLM agent previously received via
the _callback_format: structured JSON payload.

Activity-Driven Observability:
  Decomposed into named activities. See utils/observability/activity_decorator.py.
"""

import json
import sqlite3
from typing import Any
from pydantic import BaseModel, Field

from tools.base import BaseTool, ToolExecutionError, ToolValidationError
from database.connection import DatabaseManager
from utils.telegram.pipeline import PublisherPipeline
from utils.observability.activity_decorator import activity


class PublisherInput(BaseModel):
    batch_id: str = Field(..., description="The unique ULID of the batch to publish.")
    reset: bool = Field(False, description="Force full reset.")
    finalize: bool = Field(False, description="If true and batch is PARTIAL, mark it COMPLETED without re-publishing.")


INPUT_MODEL = PublisherInput

class PublisherTool(BaseTool):
    """Publisher Tool: Translates and delivers curated intelligence via Producer-Consumer pipeline."""
    
    name = "publisher"

    def is_resumable(self, args: dict[str, Any]) -> bool:
        return True

    # --- Activity-decomposed sub-methods ---

    @activity("Validate Publisher Input")
    def _validate_publisher_input(self, args: dict, job_id: str) -> tuple:
        """Extract and validate batch_id, reset, finalize from args. Raises on missing batch_id."""
        batch_id = args.get("batch_id")
        if not batch_id:
            raise ToolExecutionError(
                "batch_id is required.",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Provide a valid 'batch_id' parameter.",
            )
        reset = args.get("reset", False)
        finalize = args.get("finalize", False)
        return batch_id, reset, finalize

    @activity("Fetch Batch Info")
    def _fetch_batch_info(self, batch_id: str, job_id: str) -> dict:
        """Fetch batch info from DB. Raises if not found."""
        from database.broadcast.queries import get_batch_info
        batch_info = get_batch_info(batch_id)
        if not batch_info:
            raise ToolExecutionError(
                "Batch not found.",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Verify the batch_id is valid. If lost, use the `scraper` tool to generate a new batch.",
            )
        return batch_info

    @activity("Check Batch Completeness")
    def _check_batch_completeness(self, batch_info: dict, reset: bool, finalize: bool, batch_id: str) -> str | None:
        """Return a short-circuit markdown string if the batch is already COMPLETED (and no reset/finalize)."""
        batch_status = batch_info["status"]
        if batch_status == "COMPLETED" and not reset and not finalize:
            return f"Batch {batch_id} is already fully published."
        return None

    @activity("Finalize Batch")
    def _finalize_batch(self, batch_id: str, batch_info: dict, finalize: bool, job_id: str) -> str | None:
        """If finalize=true and batch is PARTIAL/PUBLISHING, mark COMPLETED. Returns markdown or None."""
        from database.writer import enqueue_write
        if not finalize:
            return None
        batch_status = batch_info["status"]
        if batch_status in ("PARTIAL", "PUBLISHING"):
            enqueue_write(
                "UPDATE broadcast_details SET publish_status = 'SKIPPED', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ? AND publish_status NOT IN ('PUBLISHED_ARCHIVE')",
                (batch_id,),
            )
            enqueue_write(
                "UPDATE broadcast_batches SET status = 'COMPLETED', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
                (batch_id,),
            )
            return f"Batch {batch_id} finalized to COMPLETED."
        return None

    @activity("Acquire Publishing Lock")
    def _acquire_publishing_lock(self, batch_id: str, job_id: str) -> None:
        """Atomic check-and-set to establish strict publishing lock. Raises if locked."""
        write_conn = DatabaseManager.create_write_connection()
        try:
            cursor = write_conn.execute(
                "UPDATE broadcast_batches SET status = 'PUBLISHING', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ? AND status IN ('PENDING', 'PARTIAL', 'COMPLETED')",
                (batch_id,),
            )
            write_conn.commit()
            if cursor.rowcount == 0:
                raise ToolExecutionError(
                    f"Batch {batch_id} is currently locked or in an invalid state.",
                    tool_name=self.name,
                    job_id=job_id,
                    next_steps="Try again later or check if another publisher is running on this batch.",
                )
        finally:
            write_conn.close()

    @activity("Validate Publisher Inventory")
    def _validate_inventory(self, batch_id: str, job_id: str, top_10: list) -> None:
        """Ensure top-10 articles exist. Raises if depleted."""
        from database.writer import enqueue_write
        if not top_10:
            enqueue_write(
                "UPDATE broadcast_batches SET status = 'FAILED', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
                (batch_id,),
            )
            raise ToolExecutionError(
                "Batch aborted: Top-10 is completely depleted.",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Generate a new batch using the Scraper tool.",
            )

    @activity("Execute Publish Pipeline")
    async def _run_publish_pipeline(self, pipeline: PublisherPipeline, job_id: str) -> dict:
        """Run the publisher pipeline and return the result dict."""
        return await pipeline.run_pipeline()

    @activity("Build Publisher Markdown")
    def _build_publisher_markdown(
        self, batch_id: str, result: dict, top_10: list, batch_status: str, job_id: str
    ) -> str:
        """Build the markdown summary with semantic guidance section.

        The status guidance is preserved from the old status_overrides dict.
        The LLM agent reads this markdown to decide next actions.
        """
        summary_parts = [
            f"Publisher finished: {result['archive_posted']}/{result['total_items']} published, {result['translation_failed']} failed."
        ]
        if top_10:
            summary_parts.append("\n### Published Top 10 Articles")
            for i, art in enumerate(top_10, 1):
                ulid = art.get("ulid", art.get("id", "unknown"))
                title = art.get("title", "Untitled")
                if len(title) > 120:
                    title = title[:117] + "..."
                pub_status = art.get("publish_status", "UNKNOWN")
                summary_parts.append(f"**{i}.** [{ulid}] {title} — {pub_status}")

        # Semantic guidance section — preserves the old status_overrides behavior.
        # The agent uses this to decide whether to retry, resume, or stop.
        # Without this, a PARTIAL batch looks identical to COMPLETED.
        summary_parts.append("\n---\n### Status Guidance")
        if batch_status == "PARTIAL":
            summary_parts.append(
                f"**State: PARTIAL** — Batch publishing was interrupted "
                f"(likely by Telegram Rate Limits or translation failures).\n\n"
                f"**Next Steps:** Call the `publisher` tool again using "
                f'{{"batch_id": "{batch_id}"}}. '
                f"The system will automatically resume from the last checkpoint."
            )
        elif batch_status == "COMPLETED":
            summary_parts.append(
                "**State: COMPLETED** — All items in the batch successfully translated and published.\n\n"
                "**Next Steps:** No further actions required for this batch."
            )
        elif batch_status == "FAILED":
            summary_parts.append(
                "**State: FAILED** — Fatal error during publication.\n\n"
                "**Next Steps:** Review logs. The batch has been reverted to PARTIAL status. "
                f'Call the publisher again with {{"batch_id": "{batch_id}"}} to automatically resume.'
            )

        return "\n".join(summary_parts)

    # --- Entry point ---

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        """Execute the publisher pipeline. Returns plain markdown with semantic guidance."""
        from database.writer import enqueue_write
        from database.broadcast.queries import get_batch_info, get_batch_articles
        from database.broadcast.writer import reset_batch_publish_status
        from utils.logger import get_dual_logger

        log = get_dual_logger(__name__)
        job_id = kwargs.get("job_id")

        # Step 1: Validate input.
        batch_id, reset, finalize = self._validate_publisher_input(args, job_id)

        # Step 2: Fetch batch info.
        batch_info = self._fetch_batch_info(batch_id, job_id)
        batch_status = batch_info["status"]

        # Step 3: Check if already completed (short-circuit).
        short_circuit = self._check_batch_completeness(batch_info, reset, finalize, batch_id)
        if short_circuit:
            return short_circuit

        # Step 4: Handle finalize.
        finalized = self._finalize_batch(batch_id, batch_info, finalize, job_id)
        if finalized:
            return finalized

        # Step 5: Reset if requested.
        if reset:
            reset_batch_publish_status(batch_id)

        # Load articles.
        all_articles = get_batch_articles(batch_id)
        top_10 = [a for a in all_articles if a.get("is_top10")]
        inventory = [a for a in all_articles if not a.get("is_top10")]

        log.dual_log(
            tag="Publisher:Inventory:Check",
            message=f"Loaded {len(all_articles)} articles from DB for batch {batch_id}",
            payload={"batch_id": batch_id, "total": len(all_articles), "top10": len(top_10), "inventory": len(inventory)},
        )

        # Step 6: Validate inventory.
        self._validate_inventory(batch_id, job_id, top_10)

        # Step 7: Acquire publishing lock.
        self._acquire_publishing_lock(batch_id, job_id)

        pipeline = PublisherPipeline(
            batch_id, top_10, inventory, job_id,
            resume=(batch_status in ("PENDING", "PARTIAL")),
            reset=reset,
        )

        try:
            # Step 8: Execute pipeline.
            result = await self._run_publish_pipeline(pipeline, job_id)
            batch_status = result["batch_status"]

            log.dual_log(
                tag="Publisher:Tool:Complete",
                message=f"Publisher tool completed for batch {batch_id}",
                payload={
                    "batch_id": batch_id,
                    "batch_status": batch_status,
                    "total_items": result["total_items"],
                    "archive_posted": result["archive_posted"],
                    "briefing_posted": result.get("briefing_posted", 0),
                    "translation_failed": result["translation_failed"],
                    "skipped_items": result.get("skipped_items", 0),
                    "failed_ulids": result.get("failed_ulids", []),
                    "job_id": job_id,
                    "reset": reset,
                    "finalize": finalize,
                },
            )

            # Step 9: Build markdown.
            return self._build_publisher_markdown(batch_id, result, top_10, batch_status, job_id)

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            summary = f"Publisher pipeline crashed: {str(e)[:200]}\n\nTraceback:\n{tb}"
            enqueue_write(
                "UPDATE broadcast_batches SET status = 'PARTIAL', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
                (batch_id,),
            )
            log.dual_log(
                tag="Publisher:Tool:Crashed",
                message=f"Publisher tool crashed for batch {batch_id}",
                level="ERROR",
                exc_info=e,
                payload={"batch_id": batch_id, "error": str(e)[:500], "job_id": job_id, "reset": reset},
            )
            raise ToolExecutionError(
                summary,
                tool_name=self.name,
                job_id=job_id,
                next_steps="If the issue is transient, retry with resume flag.",
            ) from e
