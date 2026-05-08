# database/diagnostics.py
"""Queue health metrics for /diagnostics endpoint"""

from database.writer import write_queue, _write_generation
from database.logs_writer import logs_write_queue, _logs_dropped_count, _logs_dropped_lock, _logs_write_generation

def get_queue_metrics() -> dict:
    """Return internal metrics for API endpoint /diagnostics"""
    with _logs_dropped_lock:
        dropped_logs = _logs_dropped_count
    return {
        "writer": {
            "queue_depth": write_queue.qsize(),
            "write_generation": _write_generation,
            "max_size": write_queue.maxsize
        },
        "logs": {
            "queue_depth": logs_write_queue.qsize(),
            "dropped_count": dropped_logs,
            "write_generation": _logs_write_generation,
            "max_size": logs_write_queue.maxsize
        }
    }
