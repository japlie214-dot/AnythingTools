"""deprecated/bot/core/weaver.py
Context Weaver for Unified Agent Instance.

Fetches execution_ledger rows by session_id in reverse chronological order,
applies the Guillotine budget limit, performs User-Proxy Role Flip, and
injects current browser state.

This is the core context assembly engine that respects the Golden Rules:
1. Execution Ledger is the Single Source of Truth
2. Agent context is bound to session_id (Session Continuity)
3. Hard limit enforced via accumulated char_count + attachment_char_count
"""

import json
from typing import List, Dict, Any, Optional
from database.connection import DatabaseManager


def build_session_context(
    session_id: str,
    mode_system_prompt: str,
    max_budget: int
) -> List[Dict[str, Any]]:
    """
    Fetch caller history, apply Vision Window & Guillotine, enforce User-Proxy.
    
    Args:
        session_id: The session identifier for context continuity
        mode_system_prompt: System prompt for the current mode
        max_budget: Hard character budget limit (including attachments)
        
    Returns:
        List of message dictionaries ready for LLM consumption
    """
    conn = DatabaseManager.get_read_connection()
    
    # Fetch by session_id in DESCENDING order (newest first)
    rows = conn.execute(
        "SELECT role, content, tool_call_id, tool_calls_json, attachment_metadata, char_count, attachment_char_count "
        "FROM execution_ledger WHERE session_id = ? ORDER BY id DESC",
        (str(session_id),)
    ).fetchall()

    raw_messages = []
    total_cost = 0
    latest_screenshot_found = False

    # Apply Vision Window and Guillotine in reverse chronological order
    for row in rows:
        meta_raw = row["attachment_metadata"]
        meta = json.loads(meta_raw) if meta_raw else {}
        
        char_c = row["char_count"]
        att_c = row["attachment_char_count"]

        # Vision Window: Keep only the latest screenshot
        if meta.get("screenshot"):
            if latest_screenshot_found:
                # This is not the latest screenshot, remove it
                meta.pop("screenshot", None)
                att_c = 0  # Recalculate cost after removal
            else:
                latest_screenshot_found = True

        # Calculate total cost for this message
        cost = char_c + att_c
        
        # Guillotine: Stop if adding this message exceeds budget
        if total_cost + cost > max_budget:
            break 
        
        total_cost += cost
        
        msg_dict = {
            "role": row["role"],
            "content": row["content"],
            "attachment_metadata": meta
        }
        # sqlite3.Row does not implement .get(); use subscripting with fallbacks
        try:
            tcid = row["tool_call_id"]
        except Exception:
            tcid = None
        if tcid:
            msg_dict["tool_call_id"] = tcid
        try:
            tcs_raw = row["tool_calls_json"]
        except Exception:
            tcs_raw = None
        if tcs_raw:
            try:
                msg_dict["tool_calls"] = json.loads(tcs_raw)
            except Exception:
                msg_dict["tool_calls"] = None
        raw_messages.append(msg_dict)

    # Reverse to get chronological order (oldest first for LLM context)
    raw_messages.reverse()
    
    # Build final message list
    final_messages = [{"role": "system", "content": mode_system_prompt}]

    # User-Proxy Role Flip: Split messages with attachments
    for msg in raw_messages:
        meta = msg.get("attachment_metadata", {})
        if meta:
            # Original message without attachments
            final_messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })
            # Synthetic User message with file metadata
            user_msg = {
                "role": "user",
                "content": "System: The following files are attached to the previous message for your analysis.",
                "attachment_metadata": meta
            }
            final_messages.append(user_msg)
        else:
            # No attachments, include as-is
            final_messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })
    
    # Inject Current Browser State dynamically
    try:
        from utils.browser_lock import browser_lock
        from utils.browser_daemon import get_or_create_driver
        if browser_lock.locked():
            driver = get_or_create_driver()
            current_url = driver.current_url
            current_title = driver.title
            final_messages.append({
                "role": "system", 
                "content": f"Current Browser State: URL [{current_url}], Title [{current_title}]"
            })
    except Exception:
        # Browser unavailable or not initialized - skip injection
        pass

    return final_messages


def get_session_cost(session_id: str, max_budget: int) -> int:
    """
    Calculate current accumulated cost for a session without building context.
    
    Useful for checking if a new operation would exceed the Guillotine limit.
    """
    conn = DatabaseManager.get_read_connection()
    rows = conn.execute(
        "SELECT char_count, attachment_char_count, attachment_metadata "
        "FROM execution_ledger WHERE session_id = ? ORDER BY id DESC",
        (str(session_id),)
    ).fetchall()

    total_cost = 0
    latest_screenshot_found = False

    for row in rows:
        meta_raw = row["attachment_metadata"]
        meta = json.loads(meta_raw) if meta_raw else {}
        
        char_c = row["char_count"]
        att_c = row["attachment_char_count"]

        # Vision Window logic applied to cost calculation
        if meta.get("screenshot"):
            if latest_screenshot_found:
                att_c = 0
            else:
                latest_screenshot_found = True

        cost = char_c + att_c
        
        if total_cost + cost > max_budget:
            break
        
        total_cost += cost

    return total_cost


def would_exceed_budget(session_id: str, additional_cost: int, max_budget: int) -> bool:
    """
    Check if adding additional_cost would exceed the Guillotine budget.
    """
    current_cost = get_session_cost(session_id, max_budget)
    return current_cost + additional_cost > max_budget


def get_current_browser_state() -> Optional[Dict[str, str]]:
    """
    Get current browser state if available.
    
    Returns:
        Dict with 'url' and 'title' keys, or None if browser unavailable
    """
    try:
        from utils.browser_lock import browser_lock
        from utils.browser_daemon import get_or_create_driver
        if browser_lock.locked():
            driver = get_or_create_driver()
            return {
                "url": driver.current_url,
                "title": driver.title
            }
    except Exception:
        pass
    return None
