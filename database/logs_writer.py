# database/logs_writer.py
"""Asynchronous log writer for logs.db with high-throughput optimizations."""
import queue
import threading
import json
import sys
import time
from datetime import datetime, timezone

from database.connection import LogsDatabaseManager
from utils.id_generator import ULID

# Bounded queue for log writes
logs_write_queue = queue.Queue(maxsize=10000)
_logs_dropped_count = 0
_logs_dropped_lock = threading.Lock()

# Shutdown event for graceful termination
logs_shutdown_event = threading.Event()

# Writer thread and synchronization
_logs_writer_thread = None
_logs_writer_lock = threading.Lock()

# Write generation counter for read connection refresh
_logs_write_generation = 0

# Sentinel value to stop the writer
_STOP = object()


def get_logs_write_generation():
    """Get the current write generation number."""
    with _logs_writer_lock:
        return _logs_write_generation


def logs_write_worker():
    """Background worker that consumes log write tasks and persists them to logs.db."""
    global _logs_write_generation
    conn = LogsDatabaseManager.create_write_connection()
    
    last_wal_checkpoint = time.monotonic()
    consecutive_errors = 0
    
    while True:
        now = time.monotonic()
        if now - last_wal_checkpoint > 1200.0:  # 20 minutes
            last_wal_checkpoint = now
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
        
        try:
            task = logs_write_queue.get(timeout=1.0)
            if task is _STOP:
                break

            sql, params = task
            # Extract job_ids touched in this write so the SSE LogNotifyBus
            # can wake subscribed projectors. The INSERT statement binds
            # job_id as the 2nd positional param (see utils/logger/core.py:141).
            # For UPDATE statements, job_id is typically the last param.
            notified_job_ids: set = set()
            try:
                # INSERT INTO logs (...) VALUES (?, ?, ?, ...) — job_id is index 1
                if sql.startswith("INSERT") and len(params) >= 2 and params[1]:
                    notified_job_ids.add(str(params[1]))
                # UPDATE ... WHERE job_id = ? — last param
                elif sql.startswith("UPDATE") and len(params) >= 1 and params[-1]:
                    notified_job_ids.add(str(params[-1]))
            except Exception:
                pass

            try:
                if sql == "__EXEC_SCRIPT__":
                    conn.executescript(params[0])
                else:
                    conn.execute(sql, params)
                conn.commit()

                # Increment generation to notify readers
                with _logs_writer_lock:
                    _logs_write_generation += 1
                consecutive_errors = 0

                # Wake SSE projectors subscribed to these job_ids.
                # Safe to call from this thread — log_notify.notify uses
                # call_soon_threadsafe internally.
                if notified_job_ids:
                    try:
                        from api.sse import log_notify
                        log_notify.notify(notified_job_ids)
                    except Exception:
                        pass
            except Exception as e:
                # Rollback; report to stderr to avoid recursive logging into logs DB
                try:
                    conn.rollback()
                except Exception as rb_err:
                    sys.stderr.write(f"[FATAL] Logs rollback failed: {rb_err}\n")

                sys.stderr.write(f"[CRITICAL] Logs writer error: {e}\n")

                consecutive_errors += 1
                if consecutive_errors >= 3:
                    sys.stderr.write("[CRITICAL] Logs connection poisoned. Attempting reconnect...\n")
                    try:
                        conn.close()
                    except Exception:
                        pass
                    try:
                        conn = LogsDatabaseManager.create_write_connection()
                        consecutive_errors = 0
                    except Exception as re_err:
                        sys.stderr.write(f"[FATAL] Logs reconnection failed: {re_err}\n")
            finally:
                try:
                    logs_write_queue.task_done()
                except Exception:
                    pass
        except queue.Empty:
            # Check if shutdown was requested
            if logs_shutdown_event.is_set():
                break
    
    try:
        conn.close()
    except Exception:
        pass


def logs_enqueue_write(sql, params=(), *, track=False):
    """Enqueue a write operation to the logs database with overflow protection."""
    # NOTE: logs.db writes are intentionally NOT gated by
    # DATABASE_INTEGRATION_ENABLED. The logs database is the observability
    # substrate — it must always capture events, even when the operational
    # DB is disabled (e.g., during health checks or staging runs).
    # Path-level isolation is handled by DATABASE_STAGING_ENABLED in
    # database/connection.py, which diverts logs.db to data/staging/logs.db.
    global _logs_writer_thread, _logs_dropped_count
    if _logs_writer_thread is None or not _logs_writer_thread.is_alive():
        start_logs_writer()
    
    try:
        logs_write_queue.put_nowait((sql, params))
    except queue.Full:
        with _logs_dropped_lock:
            _logs_dropped_count += 1
        # Dropped the log entry, do NOT terminate the application


def start_logs_writer():
    """Start the logs writer thread."""
    global _logs_writer_thread
    with _logs_writer_lock:
        if _logs_writer_thread and _logs_writer_thread.is_alive():
            return
        
        logs_shutdown_event.clear()
        _logs_writer_thread = threading.Thread(
            target=logs_write_worker,
            name="logs-writer",
            daemon=True
        )
        _logs_writer_thread.start()


def stop_logs_writer():
    """Gracefully stop the logs writer thread."""
    logs_shutdown_event.set()
    try:
        # Pass the sentinel directly, not inside a tuple
        logs_write_queue.put(_STOP, timeout=2.0)
    except queue.Full:
        pass
    
    global _logs_writer_thread
    if _logs_writer_thread:
        _logs_writer_thread.join(timeout=5.0)
        _logs_writer_thread = None


# Phase 1: Verify logger readiness by writing a test log entry and confirming persistence.
def verify_logs_readiness(timeout: float = 5.0) -> bool:
    """Write a test log entry and confirm it is persisted, setting _logger_ready.

    Returns True if the probe succeeds within the timeout, otherwise False.
    """
    from utils.id_generator import ULID
    from utils.logger.state import _logger_ready
    import time
    from datetime import datetime, timezone

    # Ensure writer thread is running
    if _logs_writer_thread is None or not _logs_writer_thread.is_alive():
        return False

    test_id = ULID.generate()
    test_ts = datetime.now(timezone.utc).isoformat()
    try:
        logs_enqueue_write(
            "INSERT INTO logs (id, job_id, tag, level, status_state, message, payload_json, event_id, error_json, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                test_id,
                None,
                "System:Readiness:Probe",
                "DEBUG",
                None,
                "Readiness probe",
                '{"action": "readiness_check"}',
                ULID.generate(),
                None,
                test_ts,
            ),
        )
        start = time.time()
        while time.time() - start < timeout:
            conn = LogsDatabaseManager.get_read_connection()
            row = conn.execute("SELECT id FROM logs WHERE id = ?", (test_id,)).fetchone()
            if row:
                _logger_ready.set()
                return True
            time.sleep(0.1)
        return False
    except Exception:
        return False
