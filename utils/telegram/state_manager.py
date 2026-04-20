# utils/telegram/state_manager.py
import json
from database.writer import enqueue_transaction
from utils.telegram.types import PhaseState

class PhaseStateManager:
    """Manages PhaseState and persists it atomically alongside job_items updates."""
    def __init__(self, batch_id: str):
        self.batch_id = batch_id
        self.state = PhaseState()

    def persist_atomic(self, job_id: str, item_metadata: str, item_status: str) -> None:
        """Enqueues a bundled transaction to update job_items and broadcast_batches."""
        phase_json = json.dumps(self.state.to_dict(), ensure_ascii=False)
        statements = [
            (
                """UPDATE job_items SET status = ?, updated_at = CURRENT_TIMESTAMP 
                   WHERE job_id = ? AND json_extract(item_metadata, '$.step') = json_extract(?, '$.step') 
                   AND json_extract(item_metadata, '$.ulid') = json_extract(?, '$.ulid')""",
                (item_status, job_id, item_metadata, item_metadata)
            ),
            (
                """UPDATE broadcast_batches SET phase_state = ?, updated_at = CURRENT_TIMESTAMP 
                   WHERE batch_id = ?""",
                (phase_json, self.batch_id)
            )
        ]
        enqueue_transaction(statements)
