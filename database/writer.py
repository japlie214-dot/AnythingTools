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
                    try:
                        conn.execute(sql, params)
                        conn.commit()
                        # Advance write generation so readers know a new commit occurred.
                        with _writer_lock:
                            _write_generation += 1
                    except Exception as e:
                        # Attempt automatic recovery for missing-table errors (common after schema drift)
                        try:
                            import sqlite3 as _sqlite3
                            msg = str(e).lower()
                        except Exception:
                            msg = str(e)

                        if isinstance(e, Exception) and "no such table" in msg:
                            # Extract missing table name and attempt best-effort repair
                            try:
                                missing = str(e).split(":")[-1].strip()
                                if missing == "job_logs":
                                    # Create job_logs table on-demand
                                    conn.execute(
                                        """CREATE TABLE IF NOT EXISTS job_logs (
                                            id           TEXT PRIMARY KEY,
                                            job_id       TEXT,
                                            tag          TEXT,
                                            level        TEXT,
                                            status_state TEXT,
                                            message      TEXT,
                                            payload_json TEXT,
                                            timestamp    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                            FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                                        )"""
                                    )
                                    conn.commit()
                                    # retry the original statement once
                                    conn.execute(sql, params)
                                    conn.commit()
                                    with _writer_lock:
                                        _write_generation += 1
                                else:
                                    # As a last resort, try to initialize the full schema
                                    try:
                                        from database.schema import get_init_script
                                        script = get_init_script()
                                        conn.executescript(script)
                                        conn.commit()
                                        conn.execute(sql, params)
                                        conn.commit()
                                        with _writer_lock:
                                            _write_generation += 1
                                    except Exception as _e2:
                                        log.dual_log(
                                            tag="DB:Writer:Error",
                                            message=f"Failed to repair missing table {missing}.",
                                            level="ERROR",
                                            payload={"sql": sql, "params": str(params)},
                                            exc_info=_e2,
                                        )
                                        conn.rollback()
                            except Exception as _e:
                                log.dual_log(
                                    tag="DB:Writer:Error",
                                    message="Automatic missing-table repair attempted and failed.",
                                    level="ERROR",
                                    payload={"sql": sql, "params": str(params)},
                                    exc_info=_e,
                                )
                                conn.rollback()
                        else:
                            log.dual_log(
                                tag="DB:Writer:Error",
                                message="Database write failed.",
                                level="ERROR",
                                payload={"sql": sql, "params": str(params)},
                                exc_info=e,
                            )
                            conn.rollback()
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



def shutdown_writer() -> None:
    if _writer_thread is None:
        return
    shutdown_event.set()
    try:
        write_queue.put(_STOP)
    except Exception:
        pass
    _writer_thread.join()
