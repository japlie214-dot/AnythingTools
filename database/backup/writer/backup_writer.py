# database/backup/writer/backup_writer.py
import queue
import threading
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Dict, Any
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

backup_write_queue: queue.Queue = queue.Queue(maxsize=5000)
_backup_writer_thread = None
_backup_shutdown = threading.Event()

@dataclass
class BackupWriteTask:
    table_name: str
    operation: str  # "UPSERT", "DELETE", "DLQ"
    records: List[Dict[str, Any]]
    pk_col: str = "id"

def backup_writer_worker(db_path: str):
    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    while not _backup_shutdown.is_set():
        try:
            task = backup_write_queue.get(timeout=1.0)
            if task is None:
                break
            try:
                if task.operation == "UPSERT" and task.records:
                    cols = list(task.records[0].keys())
                    col_str = ",".join(cols)
                    placeholders = ",".join(["?"] * len(cols))
                    sql = f"INSERT OR REPLACE INTO {task.table_name} ({col_str}) VALUES ({placeholders})"
                    tuples = [tuple(r.get(c) for c in cols) for r in task.records]
                    conn.executemany(sql, tuples)
                elif task.operation == "DELETE" and task.records:
                    sql = f"DELETE FROM {task.table_name} WHERE {task.pk_col} = ?"
                    tuples = [(r.get(task.pk_col),) for r in task.records]
                    conn.executemany(sql, tuples)
                elif task.operation == "DLQ" and task.records:
                    sql = "INSERT OR REPLACE INTO dead_letter_queue (table_name, row_id, row_data, error_message) VALUES (?, ?, ?, ?)"
                    tuples = [(task.table_name, str(r.get(task.pk_col, "")), str(r), r.get("_error_msg", "Unknown error")) for r in task.records]
                    conn.executemany(sql, tuples)
                conn.commit()
            except Exception as e:
                conn.rollback()
                log.dual_log(tag="Backup:Writer:Error", message=f"Backup write failed: {e}", level="ERROR", payload={"table": task.table_name, "operation": task.operation, "error": str(e)})
                if task.operation != "DLQ":
                    dlq_task = BackupWriteTask("dead_letter_queue", "DLQ", [{**r, "_error_msg": str(e)} for r in task.records], task.pk_col)
                    backup_write_queue.put_nowait(dlq_task)
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

def enqueue_backup_write(task: BackupWriteTask, max_retries: int = 3, retry_delay: float = 0.5):
    for attempt in range(max_retries + 1):
        try:
            backup_write_queue.put_nowait(task)
            return
        except queue.Full:
            if attempt < max_retries:
                log.dual_log(tag="Backup:Writer:QueueFullRetry", message=f"Queue full, retry {attempt+1}/{max_retries}", level="WARNING", payload={"attempt": attempt, "table": task.table_name})
                time.sleep(retry_delay * (2 ** attempt))
            else:
                log.dual_log(tag="Backup:Writer:QueueFullDrop", message=f"Write DROPPED after {max_retries} retries", level="ERROR", payload={"table": task.table_name, "operation": task.operation})
