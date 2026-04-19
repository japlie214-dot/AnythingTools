# database/migrations/v005_jobs_partial.py

import sqlite3

version = 5
description = "Add PARTIAL status to jobs table"

def up(conn: sqlite3.Connection, sqlite_vec_available: bool) -> None:
    """Rebuild jobs table to include PARTIAL in the status CHECK constraint."""
    
    conn.execute("""
        CREATE TABLE jobs_new (
            job_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            args_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(status IN ('PENDING','QUEUED','RUNNING','INTERRUPTED','PAUSED_FOR_HITL','COMPLETED','PARTIAL','FAILED','ABANDONED','CANCELLING')),
            retry_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            result_json TEXT
        )
    """)

    conn.execute("INSERT INTO jobs_new SELECT * FROM jobs")
    conn.execute("DROP TABLE jobs")
    conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_session_status ON jobs(session_id, status)")