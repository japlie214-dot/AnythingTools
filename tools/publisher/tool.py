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

        job_id = kwargs.get("job_id")
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT raw_json_path, curated_json_path, status FROM broadcast_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        
        if not row or not row["curated_json_path"] or not row["raw_json_path"]:
            return _fail("Batch not found or missing data.", "Verify the batch_id is valid. If lost, use the `scraper` tool to generate a new batch.")

        batch_status = row["status"]
        if batch_status == "COMPLETED" and not reset and not finalize:
            payload = {
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": "COMPLETED",
                "summary": f"Batch {batch_id} is already fully published.",
                "status_overrides": {
                    "COMPLETED": {
                        "description": "Batch is already published.",
                        "next_steps": "No further actions required. The batch is live.",
                        "rerunnable": False
                    }
                }
            }
            return json.dumps(payload, ensure_ascii=False)

        if finalize and batch_status == "PARTIAL":
            enqueue_write(
                "UPDATE broadcast_batches SET status = 'COMPLETED', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
                (batch_id,)
            )
            payload = {
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": "COMPLETED",
                "summary": f"Batch {batch_id} finalized to COMPLETED.",
                "status_overrides": {
                    "COMPLETED": {
                        "description": "Batch was manually finalized to COMPLETED.",
                        "next_steps": "No further actions required for this batch.",
                        "rerunnable": False
                    }
                }
            }
            return json.dumps(payload, ensure_ascii=False)
            
        try:
            with open(row["curated_json_path"], "r", encoding="utf-8") as f:
                top_10 = json.load(f)
            with open(row["raw_json_path"], "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception as e:
            return _fail(f"File read error: {e}", "Data may have been purged. Use the `scraper` tool to generate a new batch.")

        def _is_article(entry: Any) -> bool:
            return (
                isinstance(entry, dict)
                and entry.get("status") == "SUCCESS"
                and bool(entry.get("ulid"))
                and bool(entry.get("title"))
                and bool(entry.get("conclusion"))
                and (bool(entry.get("url")) or bool(entry.get("normalized_url")))
            )

        top_10_ulids = {item.get("ulid") for item in top_10 if item.get("ulid")}
        inventory = [
            v for v in (raw_data.values() if isinstance(raw_data, dict) else raw_data)
            if _is_article(v) and v.get("ulid") not in top_10_ulids
        ]

        raw_count = len(raw_data) if isinstance(raw_data, dict) else len(raw_data)
        valid_raw_count = sum(1 for v in (raw_data.values() if isinstance(raw_data, dict) else raw_data) if _is_article(v))
        
        from utils.logger import get_dual_logger
        log = get_dual_logger(__name__)
        log.dual_log(
            tag="Publisher:Inventory",
            message=f"Sanitized raw data: {valid_raw_count}/{raw_count} valid articles, {len(inventory)} inventory items.",
            payload={"batch_id": batch_id, "valid_articles": valid_raw_count, "inventory": len(inventory)}
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
            payload = {
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": batch_status,
                "summary": f"Publisher finished: {result['archive_posted']}/{result['total_items']} published, {result['translation_failed']} failed.",
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
