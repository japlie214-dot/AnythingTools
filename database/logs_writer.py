# database/logs_writer.py
"""Asynchronous log writer for logs.db with high-throughput optimizations."""
import queue
import threading
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from database.connection import LogsDatabaseManager
from utils.id_generator import ULID

# Bounded queue for log writes
logs_write_queue = queue.Queue(maxsize=10000)

# Fallback mechanism removed per contract; overflow will trigger SIGTERM.

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


# Removed fallback logging; overflow now triggers a fatal SIGTERM.


def logs_writer_worker():
    """Background worker that consumes log write tasks and persists them to logs.db."""
    global _logs_write_generation
    conn = LogsDatabaseManager.create_write_connection()
    
    while True:
        try:
            task = logs_write_queue.get(timeout=1.0)
            if task is _STOP:
                break
            
            sql, params = task
            try:
                if sql == "__EXEC_SCRIPT__":
                    conn.executescript(params[0])
                else:
                    conn.execute(sql, params)
                conn.commit()
                
                # Increment generation to notify readers
                with _logs_writer_lock:
                    _logs_write_generation += 1
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
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


def logs_enqueue_write(sql, params=()):
    """Enqueue a write operation to the logs database with overflow protection."""
    global _logs_writer_thread
    if _logs_writer_thread is None or not _logs_writer_thread.is_alive():
        start_logs_writer()
    
    try:
        logs_write_queue.put_nowait((sql, params))
    except queue.Full:
        try:
            # 5s blocking grace before fatal termination
            logs_write_queue.put((sql, params), timeout=5.0)
        except queue.Full:
            import os, signal
            sys.stderr.write("[FATAL] Logs write queue overflowed. Terminating application to prevent silent data loss.\n")
            os.kill(os.getpid(), signal.SIGTERM)


def start_logs_writer():
    """Start the logs writer thread."""
    global _logs_writer_thread
    with _logs_writer_lock:
        if _logs_writer_thread and _logs_writer_thread.is_alive():
            return
        
        logs_shutdown_event.clear()
        _logs_writer_thread = threading.Thread(
            target=logs_writer_worker,
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

# Phase 1: Verify logger readiness by writing a probe entry and confirming persistence.
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
