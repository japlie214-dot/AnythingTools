# database/schemas/jobs.py

TABLES = {
    "jobs": """CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            args_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(status IN ('PENDING','QUEUED','RUNNING','INTERRUPTED','PAUSED_FOR_HITL','COMPLETED','PARTIAL','PENDING_CALLBACK','FAILED','ABANDONED','CANCELLING','SKIPPED')),
            retry_count INTEGER NOT NULL DEFAULT 0,
            resume_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            result_json TEXT
        );
CREATE INDEX idx_jobs_session_status ON jobs(session_id, status);
""",
    "job_items": """CREATE TABLE job_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            item_metadata TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','COMPLETED','FAILED','SKIPPED')),
            input_data TEXT,
            output_data TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        );
CREATE INDEX idx_job_items_job_id ON job_items(job_id, status);
CREATE INDEX idx_job_items_metadata ON job_items(
            job_id,
            json_extract(item_metadata, '$.step'),
            json_extract(item_metadata, '$.is_top10'),
            json_extract(item_metadata, '$.ulid')
        );
""",
    "broadcast_batches": """CREATE TABLE broadcast_batches (
            batch_id TEXT PRIMARY KEY,
            target_site TEXT NOT NULL,
            article_count INTEGER NOT NULL DEFAULT 0,
            top10_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(status IN ('PENDING','PUBLISHING','PARTIAL','COMPLETED','FAILED')),
            source_job_id TEXT,
            content_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
CREATE INDEX idx_broadcast_batches_status ON broadcast_batches(status);
""",
    "broadcast_details": """CREATE TABLE broadcast_details (
            detail_id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            article_id TEXT NOT NULL,
            is_top10 INTEGER NOT NULL DEFAULT 0,
            top10_rank INTEGER,
            publish_status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(publish_status IN ('PENDING','TRANSLATING','PUBLISHED_BRIEFING','PUBLISHED_ARCHIVE','FAILED','SKIPPED')),
            translated_title TEXT,
            translated_summary TEXT,
            translated_conclusion TEXT,
            content_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(batch_id) REFERENCES broadcast_batches(batch_id) ON DELETE CASCADE,
            FOREIGN KEY(article_id) REFERENCES scraped_articles(id) ON DELETE CASCADE,
            UNIQUE(batch_id, article_id)
        );
CREATE INDEX idx_broadcast_details_batch ON broadcast_details(batch_id, publish_status);
CREATE INDEX idx_broadcast_details_article ON broadcast_details(article_id);
CREATE INDEX idx_broadcast_details_top10 ON broadcast_details(batch_id, is_top10);
""",
"sync_ledger": """CREATE TABLE sync_ledger (
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
"dead_letter_queue": """CREATE TABLE dead_letter_queue (
dlq_id TEXT PRIMARY KEY,
table_name TEXT NOT NULL,
row_id TEXT NOT NULL,
row_data TEXT NOT NULL,
error_message TEXT NOT NULL,
created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);"""
}

VEC_TABLES = {
}
