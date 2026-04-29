# database/schemas/logs.py

LOGS_TABLES = {
    "logs": """CREATE TABLE logs (
            id TEXT PRIMARY KEY,
            job_id TEXT,
            tag TEXT,
            level TEXT,
            status_state TEXT,
            message TEXT,
            payload_json TEXT,
            timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
CREATE INDEX idx_logs_job_id ON logs(job_id, timestamp);
CREATE INDEX idx_logs_timestamp ON logs(timestamp);
""",
}
