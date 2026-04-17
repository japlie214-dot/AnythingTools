"""tools/publisher/tool.py

Publisher Tool - Translates and delivers curated intelligence via Producer-Consumer pipeline.
"""

import json
import sqlite3
from typing import Any
from pydantic import BaseModel, Field

from tools.base import BaseTool
from database.connection import DatabaseManager
from utils.telegram_publisher import PublisherPipeline


class PublisherInput(BaseModel):
    batch_id: str = Field(..., description="The unique ULID of the batch to publish.")


INPUT_MODEL = PublisherInput

class PublisherTool(BaseTool):
    """Publisher Tool: Translates and delivers curated intelligence via Producer-Consumer pipeline."""
    
    name = "publisher"
    
    def is_resumable(self, args: dict[str, Any]) -> bool:
        return False

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        batch_id = args.get("batch_id")
        if not batch_id:
            raise ValueError("batch_id is required.")

        from database.writer import enqueue_write

        job_id = kwargs.get("job_id")
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT raw_json_path, curated_json_path, status FROM broadcast_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        
        if not row or not row["curated_json_path"] or not row["raw_json_path"]:
            raise ValueError("Batch not found or missing data.")

        if row["status"] == "COMPLETED":
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

        pipeline = PublisherPipeline(batch_id, top_10, inventory, job_id)
        
        enqueue_write("UPDATE broadcast_batches SET status = 'PUBLISHING', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?", (batch_id,))
        try:
            await pipeline.run_pipeline()
            enqueue_write("UPDATE broadcast_batches SET status = 'COMPLETED', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?", (batch_id,))
            return json.dumps({"status": "SUCCESS", "message": f"Batch {batch_id} published successfully."})
        except Exception as e:
            enqueue_write("UPDATE broadcast_batches SET status = 'PARTIAL', updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?", (batch_id,))
            raise e
