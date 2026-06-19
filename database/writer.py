# database/writer.py
"""Background SQLite writer with WriteReceipt synchronization and health monitoring.

This writer runs a single dedicated thread that performs all write and
transactional work on the primary application database connection. It
implements connection health detection and reconnection, periodic WAL
checkpointing, optional WriteReceipt synchronization primitives for
read-after-write guarantees, and safe queue overflow handling.
"""

import queue
import threading
import re
import time
import logging
from typing import Optional, List, Tuple
from dataclasses import dataclass, field

from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

# Public synchronization primitive used by callers that need confirmation
@dataclass
class WriteReceipt:
    _event: threading.Event = field(default_factory=threading.Event)
    _error: Optional[Exception] = None

    def wait(self, timeout: float = 45.0) -> bool:
        """Block until the write completes or timeout expires."""
        return self._event.wait(timeout=timeout)

    def resolve(self) -> None:
        self._event.set()

    def reject(self, error: Exception) -> None:
        self._error = error
        self._event.set()

    @property
    def error(self) -> Optional[Exception]:
        return self._error


# Writer queue and thread state
write_queue: queue.Queue[tuple] = queue.Queue(maxsize=1000)
shutdown_event = threading.Event()
_writer_thread: Optional[threading.Thread] = None
_write_lock = threading.Lock()
_write_generation: int = 0

# Special task markers
EXEC_SCRIPT = "__EXEC_SCRIPT__"
TRANSACTION_MARKER = "__TRANSACTION__"
MAX_REPAIR_RETRIES = 1


def _extract_table_name(error_msg: str) -> Optional[str]:
    match = re.search(r'no such table:\s*(?:\"|[\w\.]+\.)?(\w+)\"?', error_msg, re.IGNORECASE)
    return match.group(1) if match else None


def _is_no_such_table_error(error: Exception) -> bool:
    return "no such table" in str(error).lower()


def _is_foreign_key_error(error: Exception) -> bool:
    return "foreign key constraint failed" in str(error).lower()


def _is_recoverable_vec0_error(error: Exception) -> bool:
    msg = str(error).lower()
    recoverable_patterns = [
        "error opening vector blob",
        "vector_chunks",
        "could not insert a new vector chunk",
        "could not initialize",
    ]
    return any(p in msg for p in recoverable_patterns)


def _is_fatal_vec0_error(error: Exception) -> bool:
    msg = str(error).lower()
    fatal_patterns = [
        "invalid float32 vector",
        "blob length",
        "invalid float32 vector blob length",
    ]
    return any(p in msg for p in fatal_patterns)


def _is_vec0_error(error: Exception) -> bool:
    # Backwards compatible broad check
    return _is_recoverable_vec0_error(error) or _is_fatal_vec0_error(error)

def _attempt_table_repair(conn, table_name: str) -> bool:
    """Attempt best-effort repair of a missing table using repair script hooks."""
    try:
        from database.schemas import get_repair_script
    except Exception:
        return False

    script = get_repair_script(table_name)
    if not script:
        return False
    try:
        conn.executescript(script)
        conn.commit()
        log.dual_log(tag="DB:Writer:Repair", message=f"Repaired table: {table_name}", payload={"table": table_name})
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def _create_fresh_write_connection():
    # vec0 startup write-probes removed; no need to manage VEC_TABLE_NAMES here.
    return DatabaseManager.create_write_connection()

def get_write_generation() -> int:
    with _write_lock:
        return _write_generation


_STOP = object()


def db_writer_worker() -> None:
    """Main writer loop. Consumes tasks from write_queue and executes them.

    Task shapes accepted:
      - (sql: str, params: tuple)                 -> single statement
      - (WriteReceipt, sql: str, params: tuple)  -> tracked single statement
      - (WriteReceipt, EXEC_SCRIPT, (script_text,))
      - (WriteReceipt, TRANSACTION_MARKER, [(stmt, binds), ...])
    """
    global _write_generation
    conn = DatabaseManager.create_write_connection()
    try:
        conn.execute("PRAGMA wal_autocheckpoint = 0")
    except Exception:
        pass
    log.dual_log(tag="DB:Writer:Start", message="DB writer thread started.", payload={"thread": "sqlite-writer", "queue_maxsize": write_queue.maxsize})

    last_wal_checkpoint = time.monotonic()
    consecutive_errors = 0

    while True:
        now = time.monotonic()
        if now - last_wal_checkpoint > 1200.0:  # 20 minutes
            last_wal_checkpoint = now
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                # Do not attempt to log into logs DB here to avoid recursion
                try:
                    # best-effort stderr fallback
                    import sys
                    sys.stderr.write("[WARN] WAL checkpoint failed in DB writer\n")
                except Exception:
                    pass

        try:
            task = write_queue.get(timeout=1.0)
            if task is _STOP:
                write_queue.task_done()
                break

            # All tasks enqueued to the writer are normalized to 3-tuples: (receipt, sql, params)
            # receipt may be None when the caller does not request tracking.
            receipt, sql, params = task

            try:
                if sql == EXEC_SCRIPT:
                    script_text = params[0] if params else ""
                    conn.executescript(script_text)
                    conn.commit()
                    with _write_lock:
                        _write_generation += 1
                    if receipt:
                        receipt.resolve()
                    consecutive_errors = 0

                elif sql == TRANSACTION_MARKER:
                    statements = params
                    _vec0_retries = 0
                    while True:
                        try:
                            # Execute all statements in the transaction
                            for stmt, binds in statements:
                                conn.execute(stmt, binds)
                            conn.commit()
                            
                            with _write_lock:
                                _write_generation += 1
                            if receipt:
                                receipt.resolve()
                            consecutive_errors = 0
                            break
                        except Exception as tx_err:
                            try:
                                conn.rollback()
                            except Exception:
                                pass

                            if _is_recoverable_vec0_error(tx_err) and _vec0_retries < 2:
                                _vec0_retries += 1
                                delay = 0.1 * (2 ** (_vec0_retries - 1))

                                # Removed write-probe warm; relying on read-only verification and writer retry logic.
                                log.dual_log(tag="DB:Writer:Vec0Retry", message=f"vec0 transaction retry {_vec0_retries}/2", level="WARNING", payload={"delay": delay, "error": str(tx_err)})

                                import time as _time
                                _time.sleep(delay)
                                continue

                            if _is_fatal_vec0_error(tx_err):
                                log.dual_log(tag="DB:Writer:Vec0Fatal", message="Fatal vec0 data error, rejecting transaction", level="ERROR", payload={"error": str(tx_err)})

                            if receipt:
                                receipt.reject(tx_err)
                            raise


                else:
                    # Single statement with retry/repair attempts
                    for attempt in range(MAX_REPAIR_RETRIES + 1):
                        try:
                            conn.execute(sql, params)
                            conn.commit()
                            with _write_lock:
                                _write_generation += 1
                            if receipt:
                                receipt.resolve()
                            consecutive_errors = 0
                            break
                        except Exception as e:
                            # Attempt table repair on missing table errors
                            if _is_no_such_table_error(e):
                                table_name = _extract_table_name(str(e))
                                if table_name and _attempt_table_repair(conn, table_name) and attempt < MAX_REPAIR_RETRIES:
                                    continue
                            elif _is_foreign_key_error(e):
                                log.dual_log(tag="DB:Writer:FK", message="FK constraint failed", level="ERROR", payload={"sql": sql, "params": params})
                                try:
                                    conn.rollback()
                                except Exception:
                                    pass
                                if receipt:
                                    receipt.reject(e)
                                break
                            elif _is_recoverable_vec0_error(e):
                                param_summary = [f"<BLOB: {len(p)} bytes>" if isinstance(p, bytes) else p for p in params]
                                log.dual_log(tag="DB:Writer:VecError", message="sqlite-vec recoverable error", level="WARNING", payload={"sql_preview": str(sql)[:200], "params_preview": str(param_summary)[:200], "error": str(e)})
                                # Removed write warm attempts; rely on higher-level reconciliation and nuke if necessary.
                                try:
                                    conn.rollback()
                                except Exception:
                                    pass
                                if receipt:
                                    receipt.reject(e)
                                break
                            elif _is_fatal_vec0_error(e):
                                param_summary = [f"<BLOB: {len(p)} bytes>" if isinstance(p, bytes) else p for p in params]
                                log.dual_log(tag="DB:Writer:VecFatal", message="sqlite-vec fatal error", level="ERROR", payload={"sql_preview": str(sql)[:200], "params_preview": str(param_summary)[:200], "error": str(e)})
                                try:
                                    conn.rollback()
                                except Exception:
                                    pass
                                if receipt:
                                    receipt.reject(e)
                                break
    
                            # Unhandled error: reject receipt and break
                            log.dual_log(tag="DB:Writer:Error", message=f"Write failed: {e}", level="ERROR", payload={"sql_preview": str(sql)[:200], "params_preview": str(params)[:200]} , exc_info=e)
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                            if receipt:
                                receipt.reject(e)
                            break

            except Exception as e:
                # Global per-task failure handler
                try:
                    log.dual_log(tag="DB:Writer:Error", message="Database write failed.", level="ERROR", payload={"sql_preview": str(sql)[:200], "params_preview": str(params)[:200]}, exc_info=e)
                except Exception:
                    try:
                        import sys
                        sys.stderr.write(f"[ERROR] DB writer encountered error: {e}\n")
                    except Exception:
                        pass
                if receipt:
                    receipt.reject(e)
                try:
                    conn.rollback()
                except Exception:
                    pass

                consecutive_errors += 1
                if consecutive_errors >= 3:
                    try:
                        log.dual_log(tag="DB:Writer:Health", message="Connection poisoned. Reconnecting.", level="CRITICAL", payload={"consecutive_errors": consecutive_errors})
                    except Exception:
                        pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = DatabaseManager.create_write_connection()
                    consecutive_errors = 0

            finally:
                try:
                    write_queue.task_done()
                except Exception:
                    pass

        except queue.Empty:
            if shutdown_event.is_set() and write_queue.empty():
                break

    try:
        conn.close()
    except Exception:
        pass
    try:
        log.dual_log(tag="DB:Writer:Stop", message="DB writer thread stopped.", payload={"thread": "sqlite-writer", "stopped": True})
    except Exception:
        pass


def enqueue_write(sql: str, params: tuple = (), *, track: bool = False) -> Optional[WriteReceipt]:
    global _writer_thread
    # Honor the master DB integration toggle. When disabled (e.g. for
    # testing), all writes are silently skipped. This is the single
    # chokepoint for operational DB writes.
    try:
        import config
        if not getattr(config, "DATABASE_INTEGRATION_ENABLED", True):
            return None
    except ImportError:
        pass

    if _writer_thread is None or not _writer_thread.is_alive():
        try:
            start_writer()
        except Exception as e:
            try:
                log.dual_log(tag="DB:Writer:Error", message="Failed to start writer thread; write dropped.", level="ERROR", exc_info=e, payload={"error": str(e)})
            except Exception:
                pass
            return None

    receipt = WriteReceipt() if track else None
    try:
        write_queue.put_nowait((receipt, sql, params))
    except queue.Full:
        try:
            log.dual_log(tag="DB:Writer:Overflow", message="Write queue full; dropping write.", level="WARNING", payload={"sql_preview": sql[:200], "qsize": write_queue.qsize()})
        except Exception:
            pass
        if receipt:
            receipt.reject(RuntimeError("Write queue full"))
    return receipt


def enqueue_execscript(script_text: str, *, track: bool = False) -> Optional[WriteReceipt]:
    global _writer_thread
    # Honor the master DB integration toggle. When disabled (e.g. for
    # testing), all writes are silently skipped. This is the single
    # chokepoint for operational DB writes.
    try:
        import config
        if not getattr(config, "DATABASE_INTEGRATION_ENABLED", True):
            return None
    except ImportError:
        pass

    if _writer_thread is None or not _writer_thread.is_alive():
        try:
            start_writer()
        except Exception as e:
            try:
                log.dual_log(tag="DB:Writer:Error", message="Failed to start writer thread; execscript dropped.", level="ERROR", exc_info=e, payload={"error": str(e)})
            except Exception:
                pass
            return None

    receipt = WriteReceipt() if track else None
    try:
        write_queue.put_nowait((receipt, EXEC_SCRIPT, (script_text,)))
    except queue.Full:
        try:
            log.dual_log(tag="DB:Writer:Overflow", message="Write queue full; dropping execscript.", level="WARNING", payload={"script_head": script_text[:200], "qsize": write_queue.qsize()})
        except Exception:
            pass
        if receipt:
            receipt.reject(RuntimeError("Write queue full"))
    return receipt


def enqueue_transaction(statements: List[Tuple[str, Tuple]], *, track: bool = False) -> Optional[WriteReceipt]:
    global _writer_thread
    # Honor the master DB integration toggle. When disabled (e.g. for
    # testing), all writes are silently skipped. This is the single
    # chokepoint for operational DB writes.
    try:
        import config
        if not getattr(config, "DATABASE_INTEGRATION_ENABLED", True):
            return None
    except ImportError:
        pass

    if _writer_thread is None or not _writer_thread.is_alive():
        try:
            start_writer()
        except Exception:
            return None
    receipt = WriteReceipt() if track else None
    try:
        write_queue.put_nowait((receipt, TRANSACTION_MARKER, statements))
    except queue.Full:
        try:
            log.dual_log(tag="DB:Writer:Overflow", message="Write queue full; dropping transaction.", level="WARNING", payload={"qsize": write_queue.qsize(), "attempted_statements": len(statements)})
        except Exception:
            pass
        if receipt:
            receipt.reject(RuntimeError("Write queue full"))
    return receipt


def start_writer() -> threading.Thread:
    global _writer_thread
    with _write_lock:
        if _writer_thread is not None and _writer_thread.is_alive():
            return _writer_thread
        shutdown_event.clear()
        _writer_thread = threading.Thread(target=db_writer_worker, name="sqlite-writer", daemon=False)
        _writer_thread.start()
        return _writer_thread


async def wait_for_writes(timeout: Optional[float] = None) -> bool:
    import asyncio
    try:
        await asyncio.to_thread(write_queue.join)
        return True
    except Exception as e:
        try:
            log.dual_log(tag="DB:Writer:Wait", message=f"wait_for_writes failed: {e}", level="WARNING", exc_info=e, payload={"error": str(e)})
        except Exception:
            pass
        return False


def shutdown_writer() -> None:
    global _writer_thread
    if _writer_thread is None:
        return
    shutdown_event.set()
    try:
        write_queue.put(_STOP)
    except Exception:
        pass
    _writer_thread.join()
