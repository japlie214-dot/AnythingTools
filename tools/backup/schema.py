# tools/backup/schema.py
import pyarrow as pa

SCRAPED_ARTICLES_SCHEMA = pa.schema([
    pa.field("id", pa.string(), nullable=False),
    pa.field("vec_rowid", pa.int64(), nullable=False),
    pa.field("normalized_url", pa.string(), nullable=False),
    pa.field("url", pa.string(), nullable=False),
    pa.field("title", pa.string(), nullable=True),
    pa.field("conclusion", pa.string(), nullable=True),
    pa.field("summary", pa.string(), nullable=True),
    pa.field("metadata_json", pa.string(), nullable=False),
    pa.field("embedding_status", pa.string(), nullable=False),
    pa.field("scraped_at", pa.string(), nullable=False),
    pa.field("updated_at", pa.string(), nullable=False),
])

SCRAPED_ARTICLES_VEC_SCHEMA = pa.schema([
    pa.field("rowid", pa.int64(), nullable=False),
    pa.field("embedding", pa.fixed_size_binary(4096), nullable=False),
])

SCRAPED_ARTICLES_FTS_SCHEMA = pa.schema([
    pa.field("rowid", pa.int64(), nullable=False),
    pa.field("title", pa.string(), nullable=True),
    pa.field("conclusion", pa.string(), nullable=True),
    pa.field("summary", pa.string(), nullable=True),
])

LONG_TERM_MEMORIES_SCHEMA = pa.schema([
    pa.field("id", pa.int64(), nullable=False),
    pa.field("session_id", pa.string(), nullable=True),
    pa.field("agent_domain", pa.string(), nullable=True),
    pa.field("topic", pa.string(), nullable=False),
    pa.field("memory", pa.string(), nullable=False),
    pa.field("embedding", pa.binary(), nullable=True),
    pa.field("type", pa.string(), nullable=False),
    pa.field("created_at", pa.string(), nullable=False),
    pa.field("updated_at", pa.string(), nullable=False),
])

LONG_TERM_MEMORIES_VEC_SCHEMA = pa.schema([
    pa.field("rowid", pa.int64(), nullable=False),
    pa.field("embedding", pa.fixed_size_binary(4096), nullable=False),
])

TABLE_SCHEMAS = {
    "scraped_articles": SCRAPED_ARTICLES_SCHEMA,
    "scraped_articles_vec": SCRAPED_ARTICLES_VEC_SCHEMA,
    "scraped_articles_fts": SCRAPED_ARTICLES_FTS_SCHEMA,
    "long_term_memories": LONG_TERM_MEMORIES_SCHEMA,
    "long_term_memories_vec": LONG_TERM_MEMORIES_VEC_SCHEMA,
}

VECTOR_BYTE_LENGTH = 4096
FLOAT32_COUNT = 1024
