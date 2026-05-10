"""utils/vector_search.py
Vector Search Utility for RAG Integration

Provides semantic search capability using cosine similarity over
vector embeddings stored in the long_term_memories table.
"""

import asyncio
import config
import struct
import sqlite3
import concurrent.futures
import atexit
from typing import List, Tuple, Optional

try:
    import sqlite_vec
except ImportError:
    sqlite_vec = None

from clients.snowflake_client import snowflake_client
from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

# voyage-multilingual-2 produces 1024-dimensional float32 embeddings.
VOYAGE_EMBEDDING_BYTES = 1024 * 4  # 4096 bytes


def validate_embedding_bytes(emb_bytes: bytes) -> None:
    if emb_bytes is None:
        raise ValueError("Embedding bytes cannot be None")
    if not isinstance(emb_bytes, bytes):
        raise ValueError(f"Embedding must be bytes, got {type(emb_bytes).__name__}")
    if len(emb_bytes) != VOYAGE_EMBEDDING_BYTES:
        raise ValueError(f"Embedding byte length mismatch: expected {VOYAGE_EMBEDDING_BYTES}, got {len(emb_bytes)}")
    if len(emb_bytes) % 4 != 0:
        raise ValueError(f"Embedding BLOB length must be divisible by 4, got {len(emb_bytes)}")


async def generate_embedding(text: str, provider_type: str = "azure") -> bytes:
    """Generate a 1024-dimensional voyage-multilingual-2 embedding via Snowflake Cortex.

    Returns packed float32 bytes suitable for sqlite-vec MATCH queries.
    """
    try:
        emb_list = await asyncio.wait_for(snowflake_client.async_embed(text), timeout=60.0)
    except AttributeError:
        # Fallback for older clients: run sync embed in a thread with timeout
        emb_list = await asyncio.wait_for(asyncio.to_thread(snowflake_client.embed, text), timeout=60.0)
    except asyncio.TimeoutError:
        raise TimeoutError("Snowflake embedding API timed out after 60 seconds")

    if sqlite_vec and hasattr(sqlite_vec, "serialize_float32"):
        packed_bytes = sqlite_vec.serialize_float32(emb_list)
    else:
        packed_bytes = struct.pack('<1024f', *emb_list)
    
    validate_embedding_bytes(packed_bytes)
        
    log.dual_log(tag="Embed:Pack", message="Successfully packed vector", payload={"dimensions": len(emb_list), "bytes": len(packed_bytes)})
    return packed_bytes


async def retrieve_relevant_memories(
    query: str,
    agent_domain: str | None = None,
    memory_type: str | None = "Knowledge",
    limit: int = 5,
    threshold: float = 0.55,
    provider_type: str = "azure",
) -> List[Tuple[str, str]]:
    """
    Retrieves top-k memories using sqlite-vec MATCH on the vec0 companion table.
    """
    try:
        query_embedding = await generate_embedding(query, provider_type)

        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row

        sql = (
            "SELECT l.topic, l.memory, (1 - v.distance) AS similarity\n"
            "FROM long_term_memories_vec v\n"
            "JOIN long_term_memories l ON v.rowid = l.id\n"
            "WHERE v.embedding MATCH ? AND k = ?"
        )
        params = [query_embedding, limit]
        if agent_domain is not None:
            sql += " AND l.agent_domain = ?"
            params.append(agent_domain)
        if memory_type is not None:
            sql += " AND l.type = ?"
            params.append(memory_type)
        sql += " ORDER BY similarity DESC"

        rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            (row['topic'], row['memory'])
            for row in rows
            if row['similarity'] >= threshold
        ]

    except Exception as e:
        log.dual_log(
            tag="VectorSearch",
            message=f"Memory retrieval failed: {e}",
            level="ERROR",
            payload={"event_type": "vector_search.retrieval_error", "query": query},
            exc_info=e,
        )
        return []


async def store_memory_with_embedding(
    agent_domain: str | None,
    topic: str,
    memory: str,
    memory_type: str = "Knowledge",
    provider_type: str = "azure",
) -> bool:
    try:
        from database.writer import enqueue_transaction
        from datetime import datetime, timezone as _tz
        from utils.id_generator import ULID

        embedding = await generate_embedding(f"{topic}: {memory}", provider_type)

        # Compute stable integer rowid ensuring strict 63-bit positive range
        ulid_str = ULID.generate()
        _id_raw = int.from_bytes(ulid_str[:8].encode('utf-8'), 'big')
        vec_rowid = (_id_raw % 0x7FFFFFFFFFFFFFFE) + 1

        enqueue_transaction([
            ("INSERT INTO long_term_memories (id, agent_domain, topic, memory, type, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
             (vec_rowid, agent_domain, topic, memory, memory_type, datetime.now(_tz.utc).isoformat())),
            ("DELETE FROM long_term_memories_vec WHERE rowid = ?", (vec_rowid,)),
            ("INSERT INTO long_term_memories_vec (rowid, embedding) VALUES (?, ?)", (vec_rowid, embedding)),
        ])

        log.dual_log(
            tag="VectorSearch",
            message=f"Stored memory with embedding (vec_rowid={vec_rowid}): {topic}",
            payload={"event_type": "vector_search.memory_stored", "agent_domain": agent_domain},
        )
        return True

    except Exception as e:
        log.dual_log(
            tag="VectorSearch",
            message=f"Failed to store memory: {e}",
            level="ERROR",
            payload={"event_type": "vector_search.store_error", "agent_domain": agent_domain},
            exc_info=e,
        )
        return False


def get_memory_context_string(relevant_memories: List[Tuple[str, str]]) -> str:
    if not relevant_memories:
        return ""
    context_lines = ["<INSTITUTIONAL_CONTEXT>"]
    for topic, memory in relevant_memories:
        context_lines.append(f"<memory topic='{topic}'>")
        context_lines.append(memory)
        context_lines.append("</memory>")
    context_lines.append("</INSTITUTIONAL_CONTEXT>")
    return "\n".join(context_lines)


_sync_embed_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed-")
atexit.register(_sync_embed_executor.shutdown, wait=False)

def generate_embedding_sync(text: str, provider_type: str = "azure") -> bytes:
    from clients.snowflake_client import snowflake_client
    import struct

    future = _sync_embed_executor.submit(snowflake_client.embed, text)
    try:
        raw_emb = future.result(timeout=60.0)
    except concurrent.futures.TimeoutError:
        raise TimeoutError("Snowflake embedding API timed out after 60 seconds")

    if sqlite_vec and hasattr(sqlite_vec, "serialize_float32"):
        packed_bytes = sqlite_vec.serialize_float32(raw_emb)
    else:
        packed_bytes = struct.pack('<1024f', *raw_emb)
    
    validate_embedding_bytes(packed_bytes)
        
    log.dual_log(tag="Embed:Pack:Sync", message="Successfully packed vector sync", payload={"dimensions": len(raw_emb), "bytes": len(packed_bytes)})
    return packed_bytes
