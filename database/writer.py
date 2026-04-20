# database/writer.py
import queue
import threading
import re
from typing import Optional, List, Tuple

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
# Special marker for transaction bundles
TRANSACTION_MARKER = "__TRANSACTION__"
MAX_REPAIR_RETRIES = 1


def _extract_table_name(error_msg: str) -> Optional[str]:
    # Handles unquoted, quoted ("table"), and schema-qualified (main.table) names
    match = re.search(r'no such table:\s*(?:\"|[\w\.]+\.)?(\w+)\"?', error_msg, re.IGNORECASE)
    return match.group(1) if match else None


def _is_no_such_table_error(error: Exception) -> bool:
    return "no such table" in str(error).lower()


def _is_foreign_key_error(error: Exception) -> bool:
    return "foreign key constraint failed" in str(error).lower()


def _attempt_table_repair(conn, table_name: str) -> bool:
    """Strictly executes DDL repair script. Returns True on success."""
    from database.schemas import get_repair_script
    script = get_repair_script(table_name)
    if not script:
        return False
    try:
        conn.executescript(script)
        conn.commit()
        log.dual_log(tag="DB:Writer:Repair", message=f"Repaired table: {table_name}")
        return True
    except Exception:
        conn.rollback()
        return False


def get_write_generation() -> int:
    # Thread-safe snapshot token for reader visibility checks.
    with _writer_lock:
        return _write_generation

_STOP = object()


def db_writer_worker() -> None:
    """Background thread that consumes write tasks sequentially.

    Supports three task shapes on the queue:
      - (sql: str, params: tuple)  -> executed via conn.execute(sql, params)
      - (EXEC_SCRIPT, (script_text,)) -> executed via conn.executescript(script_text)
      - (TRANSACTION_MARKER, [(stmt, binds), ...]) -> BEGIN + execute each + COMMIT
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
                    script_text = params[0] if params else ""
                    conn.executescript(script_text)
                    conn.commit()
                    with _writer_lock:
                        _write_generation += 1
                elif sql == TRANSACTION_MARKER:
                    # params is a list of (statement, bindings)
                    statements = params
                    try:
                        for stmt, binds in statements:
                            conn.execute(stmt, binds)
                        conn.commit()
                        with _writer_lock:
                            _write_generation += 1
                    except Exception as tx_err:
                        conn.rollback()
                        log.dual_log(tag="DB:Writer:TxError", message=f"Transaction failed: {tx_err}", level="ERROR")
                else:
                    for attempt in range(MAX_REPAIR_RETRIES + 1):
                        try:
                            conn.execute(sql, params)
                            conn.commit()
                            with _writer_lock:
                                _write_generation += 1
                            break
                        except Exception as e:
                            if _is_no_such_table_error(e):
                                table_name = _extract_table_name(str(e))
                                if table_name and _attempt_table_repair(conn, table_name) and attempt < MAX_REPAIR_RETRIES:
                                    continue
                            elif _is_foreign_key_error(e):
                                log.dual_log(tag="DB:Writer:FK", message="FK Constraint failed", level="ERROR", payload={"sql": sql, "params": str(params)})
                                conn.rollback()
                                break
                            log.dual_log(tag="DB:Writer:Error", message=f"Write failed: {e}", level="ERROR", payload={"sql": sql, "params": str(params)})
                            conn.rollback()
                            break
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


def enqueue_transaction(statements: list[tuple[str, tuple]]) -> None:
    """Enqueue a batch of parameterized statements to be executed within a single transaction."""
    global _writer_thread
    if _writer_thread is None or not _writer_thread.is_alive():
        try:
            start_writer()
        except Exception:
            return
    try:
        write_queue.put_nowait((TRANSACTION_MARKER, statements))
    except queue.Full:
        log.dual_log(tag="DB:Writer", message="Write queue full; dropping transaction.", level="WARNING")


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
