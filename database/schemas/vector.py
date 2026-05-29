# database/schemas/vector.py

TABLES = {
    "scraped_articles": """CREATE TABLE scraped_articles (
            id TEXT NOT NULL PRIMARY KEY,
            vec_rowid INTEGER NOT NULL,
            url TEXT NOT NULL UNIQUE,
            title TEXT,
            conclusion TEXT,
            summary TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            embedding_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(embedding_status IN ('PENDING','EMBEDDED','SKIPPED')),
            scraped_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
CREATE INDEX idx_scraped_articles_url ON scraped_articles(url);
CREATE INDEX idx_scraped_articles_status ON scraped_articles(embedding_status);
CREATE INDEX idx_scraped_articles_vec_rowid ON scraped_articles(vec_rowid);
""",
}

FTS_TABLES = {
    "scraped_articles_fts": """CREATE VIRTUAL TABLE IF NOT EXISTS scraped_articles_fts USING fts5(
            title, conclusion, summary, content='scraped_articles', content_rowid='vec_rowid'
        );""",
}

VEC_TABLES = {
    "scraped_articles_vec": """CREATE VIRTUAL TABLE IF NOT EXISTS scraped_articles_vec USING vec0(embedding float[1024]);
""",
}

TRIGGERS = {
    "scraped_articles_ai": """CREATE TRIGGER IF NOT EXISTS scraped_articles_ai AFTER INSERT ON scraped_articles BEGIN
            INSERT INTO scraped_articles_fts(rowid, title, conclusion, summary)
            VALUES (new.vec_rowid, new.title, new.conclusion, new.summary);
        END;""",
    "scraped_articles_ad": """CREATE TRIGGER IF NOT EXISTS scraped_articles_ad AFTER DELETE ON scraped_articles BEGIN
            INSERT INTO scraped_articles_fts(scraped_articles_fts, rowid, title, conclusion, summary)
            VALUES ('delete', old.vec_rowid, old.title, old.conclusion, old.summary);
            DELETE FROM scraped_articles_vec WHERE rowid = old.vec_rowid;
        END;""",
    "scraped_articles_au": """CREATE TRIGGER IF NOT EXISTS scraped_articles_au AFTER UPDATE ON scraped_articles BEGIN
            INSERT INTO scraped_articles_fts(scraped_articles_fts, rowid, title, conclusion, summary)
            VALUES ('delete', old.vec_rowid, old.title, old.conclusion, old.summary);
            INSERT INTO scraped_articles_fts(rowid, title, conclusion, summary)
            VALUES (new.vec_rowid, new.title, new.conclusion, new.summary);
        END;""",
    "scraped_articles_au_vec": """CREATE TRIGGER IF NOT EXISTS scraped_articles_au_vec AFTER UPDATE ON scraped_articles
        WHEN old.vec_rowid != new.vec_rowid OR new.embedding_status = 'PENDING' BEGIN
            DELETE FROM scraped_articles_vec WHERE rowid = old.vec_rowid;
        END;""",
}
