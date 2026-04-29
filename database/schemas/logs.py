# database/schemas/logs.py

LOGS_TABLES = {
    "logs": """CREATE TABLE logs (
            -- Primary identifier (ULID)
            id TEXT PRIMARY KEY,
            -- Job context (NULL for system logs)
            job_id TEXT,
            -- Rule of Three tag
            tag TEXT NOT NULL,
            -- Log severity
            level TEXT NOT NULL,
            -- Optional job status update
            status_state TEXT,
            -- Console notification (Summary)
            message TEXT NOT NULL,
            -- COMPLETE structured detail (Database Detail)
            payload_json TEXT,
            -- Distributed tracing ID
            event_id TEXT,
            -- Structured error {type, message, traceback}
            error_json TEXT,
            -- ISO 8601 UTC timestamp
            timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
CREATE INDEX idx_logs_job_id ON logs(job_id, timestamp);
CREATE INDEX idx_logs_timestamp ON logs(timestamp);
CREATE INDEX idx_logs_tag_ts ON logs(tag, timestamp);
""",
}
