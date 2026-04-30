# database/blackboard.py
"""Blackboard service for structured job step tracking and recovery.

Implements initialization of a checklist of steps for a job, claiming a step for execution,
recording completion with output data, handling failures, and retrieving the current state.
All database writes are routed through the serialized background writer via `enqueue_write`.
"""

import json
from datetime import datetime, timezone
from typing import List, Dict, Any

from database.writer import enqueue_write
from database.connection import DatabaseManager
from utils.logger.core import get_dual_logger
from utils.metadata_helpers import make_metadata

log = get_dual_logger(__name__)


class BlackboardService:
    @staticmethod
    def initialize_checklist(job_id: str, steps: List[str]) -> None:
        """Create a pending entry for each step in `job_items`.

        Logs a SYS:BLACKBOARD:INIT entry with the step list.
        """
        log.dual_log(tag="SYS:BLACKBOARD:INIT", message=f"Initializing {len(steps)} steps", payload={"steps": steps})
        for step in steps:
            meta = make_metadata("blackboard", step)
            enqueue_write(
                "INSERT INTO job_items (job_id, item_metadata, status, updated_at) VALUES (?, ?, 'PENDING', ?)",
                (job_id, meta, datetime.now(timezone.utc).isoformat()),
            )

    @staticmethod
    def claim_step(job_id: str, step_identifier: str) -> None:
        """Mark a step as RUNNING.

        Emits a SYS:BLACKBOARD:CLAIM log entry.
        """
        log.dual_log(tag="SYS:BLACKBOARD:CLAIM", message=f"Claiming step {step_identifier}", payload={"job_id": job_id, "step": step_identifier})
        enqueue_write(
            "UPDATE job_items SET status = 'RUNNING', updated_at = ? WHERE job_id = ? "
            "AND json_extract(item_metadata, '$.step') = 'blackboard' "
            "AND json_extract(item_metadata, '$.ulid') = ?",
            (datetime.now(timezone.utc).isoformat(), job_id, step_identifier),
        )

    @staticmethod
    def complete_step(job_id: str, step_identifier: str, output_data: Dict[str, Any]) -> None:
        """Mark a step as COMPLETED and store its output JSON.

        Logs DB:WRITE:START/END around the persistence operation.
        """
        log.dual_log(tag="DB:WRITE:START", message=f"Saving results for {step_identifier}", payload=output_data)
        enqueue_write(
            "UPDATE job_items SET status = 'COMPLETED', output_data = ?, updated_at = ? WHERE job_id = ? "
            "AND json_extract(item_metadata, '$.step') = 'blackboard' "
            "AND json_extract(item_metadata, '$.ulid') = ?",
            (json.dumps(output_data), datetime.now(timezone.utc).isoformat(), job_id, step_identifier),
        )
        log.dual_log(tag="DB:WRITE:END", message=f"Step {step_identifier} persisted successfully", payload={"step": step_identifier})

    @staticmethod
    def fail_step(job_id: str, step_identifier: str, error: str) -> None:
        """Mark a step as FAILED and record the error message.

        Emits a SYS:BLACKBOARD:FAILURE log entry.
        """
        log.dual_log(tag="SYS:BLACKBOARD:FAILURE", message=f"Step {step_identifier} failed", level="ERROR", payload={"error": error})
        enqueue_write(
            "UPDATE job_items SET status = 'FAILED', output_data = ?, updated_at = ? WHERE job_id = ? "
            "AND json_extract(item_metadata, '$.step') = 'blackboard' "
            "AND json_extract(item_metadata, '$.ulid') = ?",
            (json.dumps({"error": error}), datetime.now(timezone.utc).isoformat(), job_id, step_identifier),
        )

    @staticmethod
    def get_state(job_id: str) -> List[Dict[str, Any]]:
        """Return the list of steps with their status and any stored output data.
        """
        conn = DatabaseManager.get_read_connection()
        rows = conn.execute(
            "SELECT item_metadata, status, output_data FROM job_items WHERE job_id = ?",
            (job_id,),
        ).fetchall()
        
        result = []
        for r in rows:
            d = dict(r)
            try:
                meta = json.loads(d.get("item_metadata") or "{}")
                d["step_identifier"] = meta.get("ulid") or meta.get("step") or "unknown"
            except Exception:
                d["step_identifier"] = "unknown"
            d.pop("item_metadata", None)
            result.append(d)
        return result
