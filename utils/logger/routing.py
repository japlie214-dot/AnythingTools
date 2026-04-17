# utils/logger/routing.py
from pathlib import Path

# Shared by handlers.py (handler file creation) and core.py (clear_sql_log,
# global_log_purge). Defined here — earlier in the DAG than both consumers —
# to provide a single source of truth without cross-consumer coupling.
_LOG_DIR = Path("logs")

LOG_MAP: dict[str, str] = {
    "scraper": "scraper.log",
    "draft_editor": "draft_editor.log",
    "publisher": "publisher.log",
}
