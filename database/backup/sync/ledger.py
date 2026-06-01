# database/backup/sync/ledger.py
from datetime import datetime, timezone

class SyncLedger:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS sync_ledger (
        operation_id TEXT PRIMARY KEY,
        table_name TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('LOCAL_TO_CLOUD', 'CLOUD_TO_LOCAL', 'BIDIRECTIONAL')),
        row_count INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
        started_at TEXT,
        completed_at TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_sync_ledger_state ON sync_ledger(state, table_name);
    """

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
