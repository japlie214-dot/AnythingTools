"""tools/draft_editor/tool.py

Draft Editor Tool - Deterministic SWAP list manager for Top 10 curation.

Performs purely programmatic list manipulations (internal index swap or external ULID replacement)
to ensure the Top 10 cardinality is strictly maintained.
"""

import json
import sqlite3
import tempfile
import os
from typing import Any
from pydantic import BaseModel, Field

from tools.base import BaseTool
from database.connection import DatabaseManager


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

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        batch_id = args.get("batch_id")
        operations = args.get("operations", [])
        
        if not batch_id:
            raise ValueError("batch_id is required.")

        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT raw_json_path, curated_json_path, status FROM broadcast_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        
        if not row or not row["curated_json_path"]:
            raise ValueError("Batch not found or missing curated data.")

        if row["status"] != "PENDING":
            return json.dumps({"status": "FAILED", "error": f"CRITICAL LOCK: Cannot execute SWAP on batch {batch_id}. The batch status is '{row['status']}'. Draft Editor modifications are strictly locked to 'PENDING' batches to prevent state corruption."})
            
        try:
            with open(row["curated_json_path"], "r", encoding="utf-8") as f:
                top_10 = json.load(f)
            with open(row["raw_json_path"], "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception as e:
            raise ValueError(f"File read error: {e}")

        # Process SWAP operations
        for op in operations:
            if hasattr(op, "dict"):
                op = op.dict()
            idx = op.get("index_top10")
            target = op.get("target_identifier")
            
            if idx is None or target is None or idx < 0 or idx >= len(top_10):
                continue
                
            if isinstance(target, int) and 0 <= target < len(top_10):
                # Internal SWAP
                top_10[idx], top_10[target] = top_10[target], top_10[idx]
            
            elif isinstance(target, str):
                # External SWAP (ULID)
                # Find in raw_data
                replacement = None
                if isinstance(raw_data, dict):
                    for k, v in raw_data.items():
                        if isinstance(v, dict) and v.get("ulid") == target:
                            replacement = v
                            break
                
                if replacement:
                    # Construct a slim version like the Scraper does
                    slim_replacement = {
                        "ulid": replacement.get("ulid"),
                        "normalized_url": replacement.get("normalized_url"),
                        "title": replacement.get("title", ""),
                        "conclusion": replacement.get("conclusion", "")
                    }
                    top_10[idx] = slim_replacement

        # Save updated curated list
        try:
            target_dir = os.path.dirname(row["curated_json_path"]) or None
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".tmp", dir=target_dir, encoding="utf-8") as tf:
                json.dump(top_10, tf, indent=2, ensure_ascii=False)
                tmp_name = tf.name
            os.replace(tmp_name, row["curated_json_path"])
        except Exception as e:
            raise ValueError(f"Save failed: {e}")

        # Return updated state
        return json.dumps({"batch_id": batch_id, "status": "SUCCESS", "top_10": top_10}, ensure_ascii=False)
