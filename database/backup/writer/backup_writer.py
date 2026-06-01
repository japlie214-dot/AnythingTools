# database/backup/writer/backup_writer.py
import queue
import threading
import sqlite3
from typing import List
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

backup_write_queue: queue.Queue = queue.Queue(maxsize=5000)
_backup_writer_thread = None
_backup_shutdown = threading.Event()

def backup_writer_worker(db_path: str):
    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    while not _backup_shutdown.is_set():
        try:
            task = backup_write_queue.get(timeout=1.0)
            if task is None:
                break
            sql, params = task
            try:
                if isinstance(params, list) and len(params) > 0 and isinstance(params[0], tuple):
                    conn.executemany(sql, params)
                else:
                    conn.execute(sql, params)
                conn.commit()
            except Exception as e:
                conn.rollback()
                log.dual_log(tag="Backup:Writer:Error", message=f"Backup write failed: {e}", level="ERROR", payload={"sql": sql[:100], "error": str(e)})
            finally:
                backup_write_queue.task_done()
        except queue.Empty:
            continue
    conn.close()

def start_backup_writer(db_path: str):
    global _backup_writer_thread
    if _backup_writer_thread is None or not _backup_writer_thread.is_alive():
        _backup_shutdown.clear()
        _backup_writer_thread = threading.Thread(target=backup_writer_worker, args=(db_path,), daemon=True)
        _backup_writer_thread.start()

def enqueue_backup_write(sql: str, params: tuple | list = ()):
    try:
        backup_write_queue.put_nowait((sql, params))
    except queue.Full:
        log.dual_log(tag="Backup:Writer:QueueFull", message="Backup write queue full, dropping write", level="WARNING", payload={"sql": sql[:100]})
