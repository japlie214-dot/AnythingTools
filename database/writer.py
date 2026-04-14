# database/writer.py
import queue
import threading
from typing import Optional

import config
from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

# A thread‑safe queue for write tasks. Each task is a tuple (sql, params).
# Use a bounded queue to avoid unbounded memory growth under heavy load.
write_queue: queue.Queue[tuple[str, tuple]] = queue.Queue(maxsize=1000)
shutdown_event = threading.Event()
_writer_thread: Optional[threading.Thread] = None
_writer_lock = threading.Lock()
_write_generation: int = 0

# Special marker for execute-script tasks enqueued to the writer.
EXEC_SCRIPT = "__EXEC_SCRIPT__"

def get_write_generation() -> int:
    # Thread-safe snapshot token for reader visibility checks.
    with _writer_lock:
        return _write_generation

_STOP = object()


def db_writer_worker() -> None:
    """Background thread that consumes write tasks sequentially.

    Supports two task shapes on the queue:
      - (sql: str, params: tuple)  -> executed via conn.execute(sql, params)
      - (EXEC_SCRIPT, (script_text,)) -> executed via conn.executescript(script_text)
    """
    global _write_generation
    conn = DatabaseManager.create_write_connection()
    log.dual_log(tag="DB:Writer:Start", message="DB writer thread started.")
    while True:
        try:
            task = write_queue.get(timeout=1.0)
            if task is _STOP:
                write_queue.task_done()
                break
            sql, params = task
            try:
                if sql == EXEC_SCRIPT:
                    # Execute a multi-statement SQL script atomically on the writer connection.
                    script_text = params[0] if params else ""
                    try:
                        conn.executescript(script_text)
                        conn.commit()
                        # Advance write generation so readers know a new commit occurred.
                        with _writer_lock:
                            _write_generation += 1
                    except Exception as e:
                        log.dual_log(
                                tag="DB:Writer:Error",
                                message="Database execscript failed.",
                            level="ERROR",
                            payload={"script_head": script_text[:200]},
                            exc_info=e,
                        )
                        conn.rollback()
                else:
                    conn.execute(sql, params)
                    conn.commit()
                    # Advance write generation so readers know a new commit occurred.
                    with _writer_lock:
                        _write_generation += 1
            except Exception as e:
                log.dual_log(
                    tag="DB:Writer:Error",
                    message="Database write failed.",
                    level="ERROR",
                    payload={"sql": sql, "params": str(params)},
                    exc_info=e,
                )
                conn.rollback()
            finally:
                write_queue.task_done()
        except queue.Empty:
            if shutdown_event.is_set() and write_queue.empty():
                break
    conn.close()
    log.dual_log(tag="DB:Writer:Stop", message="DB writer thread stopped.")


def enqueue_write(sql: str, params: tuple = ()) -> None:
    """Enqueue a write to be performed by the background writer thread.

    This function is resilient: if the writer thread is not running we attempt to
    start it and will log a warning rather than raising. If the queue is full we
    log a warning and drop the write to avoid unbounded memory growth.
    """
    global _writer_thread

    if _writer_thread is None or not _writer_thread.is_alive():
        log.dual_log(
            tag="DB:Writer",
            message="Writer thread not running; attempting restart.",
            level="WARNING",
        )
        try:
            start_writer()
        except Exception as e:
            log.dual_log(
                tag="DB:Writer",
                message="Failed to start writer thread; write dropped.",
                level="ERROR",
                exc_info=e,
            )
            return

    try:
        write_queue.put_nowait((sql, params))
    except queue.Full:
        log.dual_log(
            tag="DB:Writer",
            message="Write queue full; dropping non-critical write.",
            level="WARNING",
            payload={"sql_preview": sql[:200]},
        )


def enqueue_execscript(script_text: str) -> None:
    """Enqueue a multi-statement SQL script to be executed by the writer thread.

    The script is executed via `conn.executescript()` on the writer connection so
    it is performed under the single-writer guarantee and WAL semantics.
    """
    global _writer_thread

    if _writer_thread is None or not _writer_thread.is_alive():
        log.dual_log(
            tag="DB:Writer",
            message="Writer thread not running; attempting restart.",
            level="WARNING",
        )
        try:
            start_writer()
        except Exception as e:
            log.dual_log(
                tag="DB:Writer",
                message="Failed to start writer thread; execscript dropped.",
                level="ERROR",
                exc_info=e,
            )
            return

    try:
        write_queue.put_nowait((EXEC_SCRIPT, (script_text,)))
    except queue.Full:
        log.dual_log(
            tag="DB:Writer",
            message="Write queue full; dropping execscript.",
            level="WARNING",
            payload={"script_head": script_text[:200]},
        )


def start_writer() -> threading.Thread:
    global _writer_thread
    with _writer_lock:
        if _writer_thread is not None and _writer_thread.is_alive():
            return _writer_thread
        shutdown_event.clear()
        _writer_thread = threading.Thread(target=db_writer_worker, name="sqlite-writer", daemon=False)
        _writer_thread.start()
        return _writer_thread


def delete_messages_with_files(conn, where_clause: str, params: tuple) -> None:
    """
    Enforce GOLDEN RULE 5: physically delete on-disk attachment files before
    removing their execution_ledger rows from the database.

    Caller MUST set conn.row_factory = sqlite3.Row before passing the connection.
    Uses try/except OSError so a file already deleted (e.g., by a concurrent /reset)
    is silently ignored and never raises.
    """
    import os
    import sqlite3 as _sqlite3
    
    # Verify row_factory is set
    if not hasattr(conn, 'row_factory') or conn.row_factory != _sqlite3.Row:
        log.dual_log(
            tag="DB:FileCleanup",
            message="delete_messages_with_files requires conn.row_factory = sqlite3.Row",
            level="ERROR",
        )
        # Still enqueue the DELETE even if we can't read attachment_metadata safely
        enqueue_write(f"DELETE FROM execution_ledger WHERE {where_clause}", params)
        return

    try:
        rows = conn.execute(
            f"SELECT attachment_metadata FROM execution_ledger WHERE {where_clause}",
            params,
        ).fetchall()
        import json as _dw_json
        for row in rows:
            meta_raw = row["attachment_metadata"]
            if not meta_raw:
                continue
            
            try:
                meta = _dw_json.loads(meta_raw)
                # New stateful architecture: metadata is a dict of {key: path}
                if isinstance(meta, dict):
                    paths = [v for v in meta.values() if isinstance(v, str)]
                elif isinstance(meta, list):
                    paths = [p for p in meta if isinstance(p, str)]
                else:
                    paths = [meta_raw] if isinstance(meta_raw, str) else []
            except Exception:
                paths = [meta_raw] if isinstance(meta_raw, str) else []

            for path in paths:
                if path and isinstance(path, str) and os.path.exists(path):
                    try:
                        os.remove(path)
                        log.dual_log(
                            tag="DB:FileCleanup",
                            message=f"Deleted attachment: {path}",
                            level="DEBUG",
                        )
                    except OSError as e:
                        log.dual_log(
                            tag="DB:FileCleanup",
                            message=f"Could not delete attachment {path}: {e}",
                            level="WARNING",
                        )
    except Exception as e:
        log.dual_log(
            tag="DB:FileCleanup",
            message=f"Error reading attachment_metadata before deletion: {e}",
            level="ERROR",
        )
    finally:
        # Always enqueue the SQL DELETE regardless of file-cleanup outcome.
        enqueue_write(f"DELETE FROM execution_ledger WHERE {where_clause}", params)


async def wait_for_writes(timeout: float | None = None) -> bool:
    """Wait until the writer queue is drained and all tasks are completed.

    Returns True if the queue drained successfully, False on timeout or error.
    This runs the blocking join() call off the event loop to avoid blocking.
    """
    import asyncio, time
    start = time.time()
    try:
        # run blocking join in a thread-safe manner
        await asyncio.to_thread(write_queue.join)
        return True
    except Exception as e:
        log.dual_log(tag="DB:Writer", message=f"wait_for_writes failed: {e}", level="WARNING", exc_info=e)
        return False


def purge_stale_sessions(stale_days: int = 7) -> None:
    """Physically delete files and purge DB rows for sessions inactive > 7 days (Golden Rule 4)."""
    import sqlite3 as _sqlite3
    from database.connection import DatabaseManager
    conn = DatabaseManager.get_read_connection()
    conn.row_factory = _sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT session_id FROM execution_ledger GROUP BY session_id HAVING MAX(timestamp) < datetime('now', '-{stale_days} days')"
        ).fetchall()
        for r in rows:
            session_id = r['session_id']
            if session_id:
                delete_messages_with_files(conn, "session_id = ?", (session_id,))
    except Exception as e:
        log.dual_log(tag="DB:Cleanup", message=f"Failed to purge stale sessions: {e}", level="WARNING")


def shutdown_writer() -> None:
    if _writer_thread is None:
        return
    shutdown_event.set()
    try:
        write_queue.put(_STOP)
    except Exception:
        pass
    _writer_thread.join()
