# database/reader.py
import json
import sqlite3
from typing import List, Dict, Any, Optional
from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class ReaderError(Exception):
    """Raised for reader-layer errors or policy rejections."""
    pass

def _get_cursor() -> sqlite3.Cursor:
    conn = DatabaseManager.get_read_connection()
    conn.row_factory = sqlite3.Row
    return conn.cursor()

def execute_read_sql(sql: str, params: tuple = (), ensure_fresh: bool = False, allow_large_blobs: bool = False) -> List[Dict[str, Any]]:
    """Generic, safe SELECT executor that returns list[dict].

    - Only supports SELECT / WITH / PRAGMA statements and will raise otherwise.
    - If ensure_fresh=True and called from a synchronous context, this will block briefly
      by invoking the writer.wait_for_writes() helper in a new event loop. If called
      from an already-running asyncio event loop, callers MUST await database.writer.wait_for_writes() themselves.
    - If the query requests known large-payload columns the caller must set allow_large_blobs=True.
    """
    ss = sql.strip().upper()
    if not ss.startswith(("SELECT", "WITH", "PRAGMA")):
        raise ReaderError("execute_read_sql only supports read-only SELECT/WITH/PRAGMA statements.")

    # Optional freshness synchronization (sync-only)
    if ensure_fresh:
        try:
            import asyncio
            # If we're inside an event loop, we cannot call asyncio.run() here.
            loop = asyncio.get_running_loop()
            # Running loop detected — require caller to await wait_for_writes explicitly.
            raise ReaderError("ensure_fresh=True cannot be used inside an active event loop; await database.writer.wait_for_writes() first.")
        except RuntimeError:
            # No running loop — run wait_for_writes synchronously in a fresh loop.
            try:
                import asyncio
                from database.writer import wait_for_writes
                asyncio.run(wait_for_writes())
            except Exception as e:
                log.dual_log(tag="DB:Reader", message=f"ensure_fresh wait_for_writes failed: {e}", level="WARNING", exc_info=e)

    cur = _get_cursor()
    try:
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.dual_log(tag="DB:Reader", message=f"execute_read_sql failed: {e}", level="ERROR", exc_info=e)
        raise ReaderError(str(e))

def get_chat_messages(chat_id: int, limit: int) -> List[Dict[str, Any]]:
    """Return sanitized execution ledger messages (attachment_metadata parsed into list)."""
    cur = _get_cursor()
    cur.execute("""
        SELECT
            id as message_id, role, content, 0 as char_count, 0 as attachment_char_count,
            COALESCE(attachment_metadata, '[]') AS attachment_path,
            COALESCE(ledger_id, '') AS event_id
        FROM execution_ledger
        WHERE caller_id = ? ORDER BY id ASC LIMIT ?
    """, (str(chat_id), limit))

    out: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        r = dict(row)
        r['message_id'] = int(r.get('message_id', 0))
        r['char_count'] = int(r.get('char_count', 0))
        r['attachment_char_count'] = int(r.get('attachment_char_count', 0))
        # Parse attachment_metadata safely — legacy plain strings allowed.
        raw = r.get('attachment_path') or '[]'
        try:
            parsed = json.loads(raw)
            r['attachment_path'] = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            r['attachment_path'] = [raw] if raw else []
        out.append(r)
    return out

def get_unarchived_messages(chat_id: int, limit: int) -> List[Dict[str, Any]]:
    """Lightweight fetch used by Archivist to retrieve the next batch (no embeddings)."""
    cur = _get_cursor()
    cur.execute("""
        SELECT id as message_id, role, content, 0 as char_count, COALESCE(attachment_metadata, '[]') AS attachment_path
        FROM execution_ledger
        WHERE caller_id = ? ORDER BY id ASC LIMIT ?
    """, (str(chat_id), limit))
    return [dict(r) for r in cur.fetchall()]

def get_top_memories(chat_id: int, limit: int = 30) -> List[Dict[str, Any]]:
    """Return memory metadata only (no embedding BLOBs)."""
    cur = _get_cursor()
    cur.execute("""
        SELECT id, topic, memory, type, updated_at
        FROM long_term_memories
        WHERE chat_id = ? OR chat_id IS NULL
        ORDER BY updated_at DESC LIMIT ?
    """, (chat_id, limit))
    return [dict(r) for r in cur.fetchall()]

def get_job_with_steps(job_id: str) -> Optional[Dict[str, Any]]:
    """Return job with parsed args and steps structure."""
    cur = _get_cursor()
    cur.execute("SELECT job_id, chat_id, tool_name, status, COALESCE(args_json, '{}') as args_json FROM jobs WHERE job_id = ?", (job_id,))
    row = cur.fetchone()
    if not row:
        return None
    job = dict(row)
    try:
        job['args'] = json.loads(job.get('args_json') or '{}')
    except Exception:
        job['args'] = {}
    job.pop('args_json', None)
    cur.execute("SELECT step_identifier, status, COALESCE(output_data, '{}') as output_data FROM job_items WHERE job_id = ?", (job_id,))
    steps = []
    for s in cur.fetchall():
        sr = dict(s)
        try:
            sr['output'] = json.loads(sr.get('output_data') or '{}')
        except Exception:
            sr['output'] = {}
        sr.pop('output_data', None)
        steps.append(sr)
    job['steps'] = steps
    return job
