# database/migrations/v007_fts5_hybrid.py

import sqlite3

version = 7
description = "Add FTS5 virtual table and triggers for hybrid search"


def up(conn: sqlite3.Connection, sqlite_vec_available: bool) -> None:
    # Create FTS5 virtual table and triggers for scraped_articles
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_scraped_articles_vec_rowid ON scraped_articles(vec_rowid);
        
        CREATE VIRTUAL TABLE IF NOT EXISTS scraped_articles_fts USING fts5(
            title, conclusion, summary, content='scraped_articles', content_rowid='vec_rowid'
        );
        
        CREATE TRIGGER IF NOT EXISTS scraped_articles_ai AFTER INSERT ON scraped_articles BEGIN
            INSERT INTO scraped_articles_fts(rowid, title, conclusion, summary)
            VALUES (new.vec_rowid, new.title, new.conclusion, new.summary);
        END;
        
        CREATE TRIGGER IF NOT EXISTS scraped_articles_ad AFTER DELETE ON scraped_articles BEGIN
            INSERT INTO scraped_articles_fts(scraped_articles_fts, rowid, title, conclusion, summary)
            VALUES ('delete', old.vec_rowid, old.title, old.conclusion, old.summary);
        END;
        
        CREATE TRIGGER IF NOT EXISTS scraped_articles_au AFTER UPDATE ON scraped_articles BEGIN
            INSERT INTO scraped_articles_fts(scraped_articles_fts, rowid, title, conclusion, summary)
            VALUES ('delete', old.vec_rowid, old.title, old.conclusion, old.summary);
            INSERT INTO scraped_articles_fts(rowid, title, conclusion, summary)
            VALUES (new.vec_rowid, new.title, new.conclusion, new.summary);
        END;
    """)

    # Backfill existing data (guarded to only include rows with non-null title or conclusion)
    conn.execute("""
        INSERT INTO scraped_articles_fts(rowid, title, conclusion, summary)
        SELECT vec_rowid, title, conclusion, summary
        FROM scraped_articles
        WHERE (title IS NOT NULL AND title != '') OR (conclusion IS NOT NULL AND conclusion != '');
    """)
