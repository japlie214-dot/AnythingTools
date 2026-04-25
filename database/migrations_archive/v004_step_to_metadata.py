# database/migrations/v004_step_to_metadata.py
import sqlite3

version = 4
description = "Convert job_items.step_identifier to item_metadata JSON column"

def up(conn: sqlite3.Connection, sqlite_vec_available: bool) -> None:
    """Convert the old step_identifier column to item_metadata JSON."""
    # Check if the migration is needed
    columns = [row[1] for row in conn.execute("PRAGMA table_info(job_items)").fetchall()]
    if "step_identifier" not in columns:
        return

    # Execute statements individually to keep the runner's transaction alive
    conn.execute("""
        CREATE TABLE job_items_new (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            item_metadata TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','COMPLETED','FAILED')),
            input_data TEXT,
            output_data TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        )
    """)

    conn.execute("""
        INSERT INTO job_items_new (item_id, job_id, status, input_data, output_data, updated_at, item_metadata)
        SELECT item_id, job_id, status, input_data, output_data, updated_at,
        CASE
            WHEN step_identifier LIKE 'trans_%' THEN json_object('step', 'translate', 'ulid', REPLACE(step_identifier, 'trans_', ''), 'retry', 0)
            WHEN step_identifier LIKE 'pub_a_%' THEN json_object('step', 'publish_briefing', 'ulid', REPLACE(step_identifier, 'pub_a_', ''), 'is_top10', json('true'), 'retry', 0)
            WHEN step_identifier LIKE 'pub_b_%' THEN json_object('step', 'publish_archive', 'ulid', REPLACE(step_identifier, 'pub_b_', ''), 'retry', 0)
            ELSE json_object('step', 'legacy', 'ulid', step_identifier, 'retry', 0)
        END
        FROM job_items
    """)

    conn.execute("DROP TABLE job_items")
    conn.execute("ALTER TABLE job_items_new RENAME TO job_items")
    conn.execute("CREATE INDEX idx_job_items_job_id ON job_items(job_id, status)")
    conn.execute("""
        CREATE INDEX idx_job_items_metadata ON job_items(
            job_id,
            json_extract(item_metadata, '$.step'),
            json_extract(item_metadata, '$.is_top10'),
            json_extract(item_metadata, '$.ulid')
        )
    """)
