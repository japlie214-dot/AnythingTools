"""tools/publisher/tool.py

Publisher Tool - Translates and delivers curated intelligence via Producer-Consumer pipeline.
"""

import json
import sqlite3
from typing import Any
from pydantic import BaseModel, Field

from tools.base import BaseTool
from database.connection import DatabaseManager
from utils.telegram.pipeline import PublisherPipeline


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

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        def _fail(summary: str, next_steps: str) -> str:
            return json.dumps({
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": "FAILED",
                "summary": summary,
                "status_overrides": {
                    "FAILED": {
                        "description": "Publisher encountered a validation error.",
                        "next_steps": next_steps,
                        "rerunnable": False
                    }
                }
            }, ensure_ascii=False)

        batch_id = args.get("batch_id")
        if not batch_id:
            return _fail("batch_id is required.", "Provide a valid 'batch_id' parameter.")

        reset = args.get("reset", False)
        finalize = args.get("finalize", False)

        from database.writer import enqueue_write
        from database.broadcast.queries import get_batch_info, get_batch_articles
        from database.broadcast.writer import reset_batch_publish_status

        job_id = kwargs.get("job_id")
        batch_info = get_batch_info(batch_id)
        
        if not batch_info:
            return _fail("Batch not found.", "Verify the batch_id is valid. If lost, use the `scraper` tool to generate a new batch.")

        batch_status = batch_info["status"]
        if batch_status == "COMPLETED" and not reset and not finalize:
            payload = {
                "_callback_format": "structured", "tool_name": self.name, "status": "COMPLETED",
                "summary": f"Batch {batch_id} is already fully published.",
                "status_overrides": {"COMPLETED": {"description": "Batch is already published.", "next_steps": "No further actions required.", "rerunnable": False}}
            }
            return json.dumps(payload, ensure_ascii=False)

        if finalize and batch_status in ("PARTIAL", "PUBLISHING"):
            enqueue_write("UPDATE broadcast_details SET publish_status = 'SKIPPED', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ? AND publish_status NOT IN ('PUBLISHED_ARCHIVE')", (batch_id,))
            enqueue_write("UPDATE broadcast_batches SET status = 'COMPLETED', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?", (batch_id,))
            payload = {
                "_callback_format": "structured", "tool_name": self.name, "status": "COMPLETED",
                "summary": f"Batch {batch_id} finalized to COMPLETED.",
                "status_overrides": {"COMPLETED": {"description": "Batch was manually finalized.", "next_steps": "No further actions required.", "rerunnable": False}}
            }
            return json.dumps(payload, ensure_ascii=False)
            
        if reset:
            reset_batch_publish_status(batch_id)

        all_articles = get_batch_articles(batch_id)
        top_10 = [a for a in all_articles if a.get("is_top10")]
        inventory = [a for a in all_articles if not a.get("is_top10")]

        from utils.logger import get_dual_logger
        log = get_dual_logger(__name__)

        if not top_10:
            enqueue_write("UPDATE broadcast_batches SET status = 'FAILED', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?", (batch_id,))
            return _fail("Batch aborted: Top-10 is completely depleted.", "Generate a new batch using the Scraper tool.")

        log.dual_log(
            tag="Publisher:Inventory:Check",
            message=f"Loaded {len(all_articles)} articles from DB for batch {batch_id}",
            payload={"batch_id": batch_id, "total": len(all_articles), "top10": len(top_10), "inventory": len(inventory)}
        )

        # Atomic check-and-set to establish strict publishing lock
        write_conn = DatabaseManager.create_write_connection()
        try:
            cursor = write_conn.execute(
                "UPDATE broadcast_batches SET status = 'PUBLISHING', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ? AND status IN ('PENDING', 'PARTIAL', 'COMPLETED')",
                (batch_id,)
            )
            write_conn.commit()
            if cursor.rowcount == 0:
                payload = {
                    "_callback_format": "structured",
                    "tool_name": self.name,
                    "status": "FAILED",
                    "summary": f"Batch {batch_id} is currently locked or in an invalid state.",
                    "status_overrides": {
                        "FAILED": {
                            "description": "Publisher could not acquire lock.",
                            "next_steps": "Try again later or check if another publisher is running on this batch.",
                            "rerunnable": True
                        }
                    }
                }
                return json.dumps(payload, ensure_ascii=False)
        finally:
            write_conn.close()

        pipeline = PublisherPipeline(batch_id, top_10, inventory, job_id, resume=(batch_status in ("PENDING", "PARTIAL")), reset=reset)
        
        try:
            result = await pipeline.run_pipeline()
            batch_status = result["batch_status"]
            
            summary_parts = [f"Publisher finished: {result['archive_posted']}/{result['total_items']} published, {result['translation_failed']} failed."]
            if top_10:
                summary_parts.append("\n### Published Top 10 Articles")
                for i, art in enumerate(top_10, 1):
                    ulid = art.get("ulid", art.get("id", "unknown"))
                    title = art.get("title", "Untitled")
                    if len(title) > 120: title = title[:117] + "..."
                    pub_status = art.get("publish_status", "UNKNOWN")
                    summary_parts.append(f"**{i}.** [{ulid}] {title} — {pub_status}")

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
                }
            )

            payload = {
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": batch_status,
                "summary": "\n".join(summary_parts),
                "details": result,
                "status_overrides": {
                    "PARTIAL": {
                        "description": "Batch publishing was interrupted (likely by Telegram Rate Limits or translation failures).",
                        "next_steps": f"Call the `publisher` tool again using {{\"batch_id\": \"{batch_id}\"}}. The system will automatically resume from the last checkpoint.",
                        "rerunnable": True
                    },
                    "COMPLETED": {
                        "description": "All items in the batch successfully translated and published.",
                        "next_steps": "No further actions required for this batch.",
                        "rerunnable": False
                    },
                    "FAILED": {
                        "description": "Fatal error during publication.",
                        "next_steps": "Review logs. The batch has been reverted to PARTIAL status. Call the publisher again to automatically resume.",
                        "rerunnable": True
                    }
                }
            }
            return json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            summary = f"Publisher pipeline crashed: {str(e)[:200]}\n\nTraceback:\n{tb}"
            # Drop lock and revert to PARTIAL if an extreme rate limit or crash aborts the job
            enqueue_write("UPDATE broadcast_batches SET status = 'PARTIAL', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?", (batch_id,))
            
            log.dual_log(
                tag="Publisher:Tool:Crashed",
                message=f"Publisher tool crashed for batch {batch_id}",
                level="ERROR",
                exc_info=e,
                payload={
                    "batch_id": batch_id,
                    "error": str(e)[:500],
                    "job_id": job_id,
                    "reset": reset,
                }
            )

            payload = {
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": "FAILED",
                "summary": summary,
                "status_overrides": {
                    "FAILED": {
                        "description": "Pipeline crashed. Review logs.",
                        "next_steps": "If the issue is transient, retry with resume flag.",
                        "rerunnable": True
                    }
                }
            }
            return json.dumps(payload, ensure_ascii=False)
