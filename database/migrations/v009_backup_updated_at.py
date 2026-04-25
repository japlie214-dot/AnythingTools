# database/migrations/v009_backup_updated_at.py

import sqlite3

version = 9
description = "Add updated_at column and trigger to scraped_articles for backup deltas"

def up(conn: sqlite3.Connection, sqlite_vec_available: bool) -> None:
    # 1. Check if column already exists to ensure idempotency
    columns = [row[1] for row in conn.execute("PRAGMA table_info(scraped_articles)").fetchall()]
    if "updated_at" not in columns:
        conn.execute("ALTER TABLE scraped_articles ADD COLUMN updated_at TEXT")
        conn.execute("UPDATE scraped_articles SET updated_at = scraped_at WHERE updated_at IS NULL")

    # 2. Create the auto-update trigger
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS scraped_articles_updated_at_trigger
        AFTER UPDATE ON scraped_articles
        BEGIN
            UPDATE scraped_articles SET updated_at = CURRENT_TIMESTAMP
            WHERE id = NEW.id AND OLD.updated_at = NEW.updated_at;
        END;
    """)
