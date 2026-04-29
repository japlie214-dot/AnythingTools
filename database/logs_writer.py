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
logs_write_queue = queue.Queue(maxsize=5000)

# Fallback lock for writing to persistent fallback log
_fallback_lock = threading.Lock()

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


def _write_fallback_log(sql, params):
    """Last-resort persistent audit trail when queue overflows."""
    try:
        f_dir = Path("logs")
        f_dir.mkdir(parents=True, exist_ok=True)
        f_path = f_dir / "fallback.log"
        entry = {
            "fallback_ts": datetime.now(timezone.utc).isoformat(),
            "event_id": ULID.generate(),
            "sql": sql,
            "params": params,
        }
        with _fallback_lock:
            with open(f_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        sys.stderr.write(f"[CRITICAL:LOG_FALLBACK_FAILED] {e}\n")


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
            # 5s blocking grace before fallback
            logs_write_queue.put((sql, params), timeout=5.0)
        except queue.Full:
            _write_fallback_log(sql, params)


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
