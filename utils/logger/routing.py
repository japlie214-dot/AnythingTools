# utils/logger/routing.py
from pathlib import Path

# Shared by handlers.py and core.py. Keep _LOG_DIR, remove LOG_MAP which
# implemented specialized file-routing. The dual-logger now writes only to
# console + logs.db; any persistent file routing was removed by design.
_LOG_DIR = Path("logs")

