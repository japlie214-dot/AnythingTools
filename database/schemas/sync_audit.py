# database/schemas/sync_audit.py

TABLES = {
    "sync_ledger": """CREATE TABLE IF NOT EXISTS sync_ledger (
        operation_id TEXT PRIMARY KEY,
        table_name TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('LOCAL_TO_CLOUD', 'CLOUD_TO_LOCAL', 'BIDIRECTIONAL')),
        row_count INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
        started_at TEXT,
        completed_at TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );""",
    "dead_letter_queue": """CREATE TABLE IF NOT EXISTS dead_letter_queue (
        dlq_id TEXT PRIMARY KEY,
        table_name TEXT NOT NULL,
        row_id TEXT NOT NULL,
        row_data TEXT NOT NULL,
        error_message TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );"""
}
