# database/schemas/jobs.py

TABLES = {
    "jobs": """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            args_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(status IN ('PENDING','QUEUED','RUNNING','INTERRUPTED','PAUSED_FOR_HITL','COMPLETED','FAILED','ABANDONED','CANCELLING')),
            retry_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            result_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_session_status ON jobs(session_id, status);
    """,
    "job_items": """
        CREATE TABLE IF NOT EXISTS job_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            step_identifier TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','COMPLETED','FAILED')),
            input_data TEXT,
            output_data TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_job_items_job_id ON job_items(job_id, status);
    """,
    "job_logs": """
        CREATE TABLE IF NOT EXISTS job_logs (
            id TEXT PRIMARY KEY,
            job_id TEXT,
            tag TEXT,
            level TEXT,
            status_state TEXT,
            message TEXT,
            payload_json TEXT,
            timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_job_logs_job_id ON job_logs(job_id, timestamp);
    """,
    "broadcast_batches": """
        CREATE TABLE IF NOT EXISTS broadcast_batches (
            batch_id TEXT PRIMARY KEY,
            target_site TEXT NOT NULL,
            raw_json_path TEXT NOT NULL,
            curated_json_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(status IN ('PENDING','PUBLISHING','PARTIAL','COMPLETED','FAILED')),
            posted_research_ulids TEXT NOT NULL DEFAULT '[]',
            posted_summary_ulids TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_broadcast_batches_status ON broadcast_batches(status);
    """
}
VEC_TABLES = {}
