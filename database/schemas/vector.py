# database/schemas/vector.py

TABLES = {
    "scraped_articles": """
        CREATE TABLE IF NOT EXISTS scraped_articles (
            id TEXT NOT NULL PRIMARY KEY,
            vec_rowid INTEGER NOT NULL,
            normalized_url TEXT NOT NULL UNIQUE,
            url TEXT NOT NULL,
            title TEXT,
            conclusion TEXT,
            summary TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            embedding_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(embedding_status IN ('PENDING','EMBEDDED','SKIPPED')),
            scraped_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_scraped_articles_norm_url ON scraped_articles(normalized_url);
        CREATE INDEX IF NOT EXISTS idx_scraped_articles_status ON scraped_articles(embedding_status);
        CREATE INDEX IF NOT EXISTS idx_scraped_articles_vec_rowid ON scraped_articles(vec_rowid);
    """,
    "long_term_memories": """
        CREATE TABLE IF NOT EXISTS long_term_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            agent_domain TEXT,
            topic TEXT NOT NULL,
            memory TEXT NOT NULL,
            embedding BLOB,
            type TEXT NOT NULL DEFAULT 'Knowledge',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_memories_agent_domain ON long_term_memories(agent_domain, type, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_session_type ON long_term_memories(session_id, type, created_at DESC);
    """,
    "scraped_articles_fts": """
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
    """
}

VEC_TABLES = {
    "scraped_articles_vec": """
        CREATE VIRTUAL TABLE IF NOT EXISTS scraped_articles_vec USING vec0(
            embedding float[1024]
        );
    """,
    "long_term_memories_vec": """
        CREATE VIRTUAL TABLE IF NOT EXISTS long_term_memories_vec USING vec0(
            embedding float[1024]
        );
    """
}
