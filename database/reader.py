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
                log.dual_log(tag="DB:Reader", message=f"ensure_fresh wait_for_writes failed: {e}", level="WARNING", exc_info=e, payload={"error": str(e)})

    cur = _get_cursor()
    try:
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.dual_log(tag="DB:Reader", message=f"execute_read_sql failed: {e}", level="ERROR", exc_info=e, payload={"sql": sql, "params": params, "error": str(e)})
        raise ReaderError(str(e))
def get_job_with_steps(job_id: str) -> Optional[Dict[str, Any]]:
    """Return job with parsed args and steps structure."""
    cur = _get_cursor()
    cur.execute("SELECT job_id, session_id, tool_name, status, COALESCE(args_json, '{}') as args_json FROM jobs WHERE job_id = ?", (job_id,))
    row = cur.fetchone()
    if not row:
        return None
    job = dict(row)
    try:
        job['args'] = json.loads(job.get('args_json') or '{}')
    except Exception:
        job['args'] = {}
    job.pop('args_json', None)
    cur.execute("SELECT item_metadata, status, COALESCE(output_data, '{}') as output_data FROM job_items WHERE job_id = ?", (job_id,))
    steps = []
    for s in cur.fetchall():
        sr = dict(s)
        
        metadata_str = sr.get('item_metadata')
        if metadata_str:
            try:
                sr['metadata'] = json.loads(metadata_str)
                sr['step'] = sr['metadata'].get('step', 'unknown')
                sr['ulid'] = sr['metadata'].get('ulid', '')
                sr['retry'] = sr['metadata'].get('retry', 0)
                sr['error'] = sr['metadata'].get('error')
                sr['is_top10'] = sr['metadata'].get('is_top10', False)
            except json.JSONDecodeError:
                sr['metadata'] = {}
                sr['step'] = 'unknown'
                sr['ulid'] = ''
        else:
            sr['metadata'] = {}
            sr['step'] = 'unknown'
            sr['ulid'] = ''
            
        try:
            sr['output'] = json.loads(sr.get('output_data') or '{}')
        except Exception:
            sr['output'] = {}
        sr.pop('output_data', None)
        steps.append(sr)
    job['steps'] = steps


def get_top10_items(job_id: str) -> List[Dict[str, Any]]:
    cur = _get_cursor()
    cur.execute("""
        SELECT item_metadata, status, COALESCE(output_data, '{}') as output_data
        FROM job_items
        WHERE job_id = ?
        AND json_extract(item_metadata, '$.is_top10') = 1
        AND json_extract(item_metadata, '$.step') = 'translate'
        AND status = 'COMPLETED'
        ORDER BY item_id
        LIMIT 10
    """, (job_id,))
    
    results = []
    for row in cur.fetchall():
        try:
            metadata = json.loads(row['item_metadata'] or '{}')
            output = json.loads(row['output_data'] or '{}')
            results.append({
                'ulid': metadata.get('ulid', ''),
                'metadata': metadata,
                'output': output
            })
        except json.JSONDecodeError:
            continue
    return results


def get_all_translated_items(job_id: str) -> List[Dict[str, Any]]:
    cur = _get_cursor()
    cur.execute("""
        SELECT item_metadata, status, COALESCE(output_data, '{}') as output_data
        FROM job_items
        WHERE job_id = ?
        AND json_extract(item_metadata, '$.step') = 'translate'
        AND status = 'COMPLETED'
        ORDER BY item_id
    """, (job_id,))
    
    results = []
    for row in cur.fetchall():
        try:
            metadata = json.loads(row['item_metadata'] or '{}')
            output = json.loads(row['output_data'] or '{}')
            results.append({
                'ulid': metadata.get('ulid', ''),
                'metadata': metadata,
                'is_top10': metadata.get('is_top10', False),
                'output': output
            })
        except json.JSONDecodeError:
            continue
    return results


def get_batch_phase_state(batch_id: str) -> Dict[str, Any]:
    """Retrieve the granular phase_state mapping for a broadcast batch."""
    cur = _get_cursor()
    cur.execute(
        "SELECT phase_state FROM broadcast_batches WHERE batch_id = ?",
        (batch_id,)
    )
    row = cur.fetchone()
    
    default_state = {
        "validate": {},
        "translate": {},
        "publish_briefing": {},
        "publish_archive": {}
    }
    
    if not row or not row["phase_state"]:
        return default_state

    try:
        loaded = json.loads(row["phase_state"])
        default_state.update(loaded)
        return default_state
    except json.JSONDecodeError:
        return default_state
