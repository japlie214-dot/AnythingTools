# utils/vector_search.py
"""
Vector Search Utility for RAG Integration

Provides semantic search capabilities using cosine similarity over
vector embeddings stored in the long_term_memories table.
"""

import asyncio
import config
import struct
import sqlite3
from typing import List, Tuple, Optional
from clients.snowflake_client import snowflake_client
from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

# voyage-multilingual-2 produces 1024-dimensional float32 embeddings.
VOYAGE_EMBEDDING_BYTES = 1024 * 4  # 4096 bytes


async def generate_embedding(text: str, provider_type: str = "azure") -> bytes:
    """Generate a 1024-dimensional voyage-multilingual-2 embedding via Snowflake Cortex.

    The `provider_type` parameter is retained for call-site API compatibility
    but is not used; all embeddings are generated via Snowflake regardless of its value.

    Returns:
        Raw bytes (struct.pack float32[1024]) compatible with sqlite-vec MATCH queries.
    """
    # Use the async wrapper if available to avoid blocking the event loop
    try:
        emb_list = await snowflake_client.async_embed(text)
    except AttributeError:
        # Fallback for older clients: run sync embed in a thread
        emb_list = await asyncio.to_thread(snowflake_client.embed, text)
    return struct.pack(f'{len(emb_list)}f', *emb_list)


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

    Supports optional agent_domain and optional memory_type filters.
    """
    try:
        query_embedding = await generate_embedding(query, provider_type)

        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row

        # Build SQL dynamically based on provided filters
        sql = """
            SELECT l.topic, l.memory, (1 - v.distance) AS similarity
            FROM long_term_memories_vec v
            JOIN long_term_memories l ON v.rowid = l.id
            WHERE v.embedding MATCH ? AND k = ?
        """
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
    """
    Store a memory and its embedding in a paired-write fashion:
    - Generates a ULID-based stable integer `vec_rowid` used as the explicit id for
      long_term_memories and as the rowid in long_term_memories_vec.

    Returns True on successful enqueue of both writes.
    """
    try:
        from database.writer import enqueue_write
        from datetime import datetime, timezone as _tz
        from utils.id_generator import ULID

        embedding = await generate_embedding(f"{topic}: {memory}", provider_type)

        # Compute stable integer rowid from a new ULID string
        vec_rowid = abs(hash(ULID.generate())) % (2**63)

        enqueue_write(
            "INSERT INTO long_term_memories (id, agent_domain, topic, memory, type, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (vec_rowid, agent_domain, topic, memory, memory_type, datetime.now(_tz.utc).isoformat()),
        )
        enqueue_write(
            "INSERT INTO long_term_memories_vec (rowid, embedding) VALUES (?, ?)",
            (vec_rowid, embedding),
        )

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
            payload={"event_type": "vector_search.storage_error", "agent_domain": agent_domain},
            exc_info=e,
        )
        return False


def get_memory_context_string(relevant_memories: List[Tuple[str, str]]) -> str:
    """
    Format retrieved memories as a context string for prompts.
    
    Args:
        relevant_memories: List of (topic, memory) tuples
        
    Returns:
        Formatted context string
    """
    if not relevant_memories:
        return ""
    
    context_lines = ["<INSTITUTIONAL_CONTEXT>"]
    for topic, memory in relevant_memories:
        context_lines.append(f"<memory topic='{topic}'>")
        context_lines.append(memory)
        context_lines.append("</memory>")
    context_lines.append("</INSTITUTIONAL_CONTEXT>")
    
    return "\n".join(context_lines)
