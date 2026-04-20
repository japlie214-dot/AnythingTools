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
    resume: bool = Field(False, description="Resume from last checkpoint.")
    reset: bool = Field(False, description="Force full reset.")


INPUT_MODEL = PublisherInput

class PublisherTool(BaseTool):
    """Publisher Tool: Translates and delivers curated intelligence via Producer-Consumer pipeline."""
    
    name = "publisher"
    
    def is_resumable(self, args: dict[str, Any]) -> bool:
        return True

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        batch_id = args.get("batch_id")
        if not batch_id:
            raise ValueError("batch_id is required.")

        resume = args.get("resume", False)
        reset = args.get("reset", False)

        from database.writer import enqueue_write

        job_id = kwargs.get("job_id")
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT raw_json_path, curated_json_path, status FROM broadcast_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        
        if not row or not row["curated_json_path"] or not row["raw_json_path"]:
            raise ValueError("Batch not found or missing data.")

        if row["status"] == "COMPLETED" and not reset:
            return json.dumps({"status": "SUCCESS", "message": f"Batch {batch_id} is already fully published."})
            
        try:
            with open(row["curated_json_path"], "r", encoding="utf-8") as f:
                top_10 = json.load(f)
            with open(row["raw_json_path"], "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception as e:
            raise ValueError(f"File read error: {e}")

        top_10_ulids = {item.get("ulid") for item in top_10}
        inventory = []
        for v in (raw_data.values() if isinstance(raw_data, dict) else raw_data):
            if isinstance(v, dict) and v.get("ulid") not in top_10_ulids:
                inventory.append(v)

        # Atomic check-and-set to establish strict publishing lock
        write_conn = DatabaseManager.create_write_connection()
        try:
            cursor = write_conn.execute(
                "UPDATE broadcast_batches SET status = 'PUBLISHING', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ? AND status IN ('PENDING', 'PARTIAL', 'COMPLETED')",
                (batch_id,)
            )
            write_conn.commit()
            if cursor.rowcount == 0:
                return json.dumps({"status": "FAILED", "message": f"Batch {batch_id} is currently locked or in an invalid state."})
        finally:
            write_conn.close()

        pipeline = PublisherPipeline(batch_id, top_10, inventory, job_id, resume=resume, reset=reset)
        
        try:
            result = await pipeline.run_pipeline()
            return json.dumps({
                "status": result["batch_status"],
                "message": f"Batch {batch_id}: {result['archive_posted']}/{result['total_items']} published, {result['translation_failed']} failed",
                **result
            }, ensure_ascii=False)
        except Exception as e:
            # Drop lock and revert to PARTIAL if an extreme rate limit or crash aborts the job
            enqueue_write("UPDATE broadcast_batches SET status = 'PARTIAL', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?", (batch_id,))
            raise e
