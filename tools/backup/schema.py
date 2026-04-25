# tools/backup/schema.py
import pyarrow as pa

ARTICLES_SCHEMA = pa.schema([
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
    pa.field("operation", pa.string(), nullable=False),
])

VECTORS_SCHEMA = pa.schema([
    pa.field("rowid", pa.int64(), nullable=False),
    pa.field("embedding", pa.fixed_size_binary(4096), nullable=False),
    pa.field("article_id", pa.string(), nullable=False),
])

VECTOR_BYTE_LENGTH = 4096
FLOAT32_COUNT = 1024
