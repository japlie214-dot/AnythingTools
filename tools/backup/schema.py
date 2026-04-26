# tools/backup/schema.py
import pyarrow as pa

VECTOR_BYTE_LENGTH = 4096
FLOAT32_COUNT = 1024

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
    pa.field("embedding", pa.binary(VECTOR_BYTE_LENGTH), nullable=False),
])

# FTS schema intentionally omitted: derived/external FTS tables must not be backed up or restored directly.
# They are rebuilt post-restore using the canonical content tables and index rebuild steps.

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
    pa.field("embedding", pa.binary(VECTOR_BYTE_LENGTH), nullable=False),
])

TABLE_SCHEMAS = {
    "scraped_articles": SCRAPED_ARTICLES_SCHEMA,
    "scraped_articles_vec": SCRAPED_ARTICLES_VEC_SCHEMA,
    "long_term_memories": LONG_TERM_MEMORIES_SCHEMA,
    "long_term_memories_vec": LONG_TERM_MEMORIES_VEC_SCHEMA,
}

def validate_embedding_bytes(embedding_bytes: bytes, expected_length: int = VECTOR_BYTE_LENGTH) -> None:
    """Validate that embedding bytes match the expected fixed size."""
    if embedding_bytes is None:
        raise ValueError("Embedding bytes cannot be None for a non-nullable vector field")
    actual = len(embedding_bytes)
    if actual != expected_length:
        raise ValueError(
            f"Embedding size mismatch: expected {expected_length} bytes "
            f"({expected_length // 4} float32s), got {actual} bytes."
        )
