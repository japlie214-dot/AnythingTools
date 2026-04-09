# bot/orchestrator/context.py
"""Context assembly from execution ledger.

Builds the message list for the LLM from the `execution_ledger`, enforcing:
1. Vision Window: only the latest screenshot for a specific job_id may be kept.
2. User-Proxy rule: attachments/images must be presented to the LLM inside a
   User-role message.
3. Budget enforcement (via eviction).

This module is designed to be called by an orchestrator or worker that needs to
assemble a message list for the LLM. It does NOT import REGISTRY; it focuses
solely on context construction from the persistent ledger.
"""

import json
from typing import List, Dict, Any

from database.connection import DatabaseManager
from bot.orchestrator.eviction import enforce_budget


def build_context(job_id: str, max_budget: int) -> List[Dict[str, Any]]:
    """Assemble context messages for a job.

    Steps:
    1. Fetch ledger entries for the job (ordered by id).
    2. Find the latest screenshot index (across all rows).
    3. For each row, strip the 'screenshot' attachment unless it is the latest.
       Keep other attachment types intact.
    4. Compute per-row costs.
    5. Wrap attachments inside a User role message (User-Proxy rule).
    6. Enforce budget via FIFO eviction, keeping the system prompt if present.
    """
    conn = DatabaseManager.get_read_connection()
    rows = conn.execute(
        "SELECT role, content, attachment_metadata FROM execution_ledger WHERE job_id = ? ORDER BY id ASC",
        (job_id,),
    ).fetchall()

    raw_messages: List[Dict[str, Any]] = []
    latest_screenshot_idx = -1

    # Vision Window pass 1: find latest screenshot index
    for i, row in enumerate(rows):
        meta = json.loads(row["attachment_metadata"]) if row["attachment_metadata"] else {}
        if meta.get("screenshot"):
            latest_screenshot_idx = i

    # Build raw messages
    for i, row in enumerate(rows):
        meta_raw = row["attachment_metadata"]
        meta = json.loads(meta_raw) if meta_raw else {}
        
        # Vision Window: strip screenshot if not the latest
        if meta.get("screenshot") and i != latest_screenshot_idx:
            meta.pop("screenshot", None)

        # Only count actual file attachments toward the token budget
        # Valid attachments are strings pointing to files (mapped in clients/llm/types.py)
        from clients.llm.types import MIME_TYPE_MAP
        import os
        
        actual_attachments = {
            k: v for k, v in meta.items()
            if isinstance(v, str) and os.path.splitext(v)[1].lower() in MIME_TYPE_MAP
        }

        raw_messages.append({
            "role": row["role"],
            "content": row["content"],
            "attachment_metadata": actual_attachments,
            "char_count": len(row["content"]),
            "attachment_char_count": sum(50000 for _ in actual_attachments.keys()),
        })

    # User-Proxy rule: wrap attachments in a User message
    messages: List[Dict[str, Any]] = []
    for msg in raw_messages:
        meta = msg.get("attachment_metadata", {})
        if meta:
            # Create a separate user message containing the attachments
            user_msg = {
                "role": "user",
                "content": "Attachments attached to previous message.",
                "attachment_metadata": meta,
                "char_count": 0,
                "attachment_char_count": msg["attachment_char_count"],
            }
            # First the original message (without attachments)
            messages.append({
                "role": msg["role"],
                "content": msg["content"],
                "attachment_metadata": {},
                "char_count": msg["char_count"],
                "attachment_char_count": 0,
            })
            # Followed by the User message holding attachments
            messages.append(user_msg)
        else:
            messages.append(msg)

    return enforce_budget(messages, max_budget)
