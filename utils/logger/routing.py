# utils/logger/routing.py
from pathlib import Path

# Spool directory for oversized payloads.
# Created lazily on first spool in formatters.py, but defined here as a constant.
_LOG_SPOOL_DIR = Path("artifacts") / "log_spool"

# Ensure the spool directory exists immediately on import to avoid
# race conditions with fast-firing logs during startup.
_LOG_SPOOL_DIR.mkdir(parents=True, exist_ok=True)

# Shared by handlers.py and core.py. Keep _LOG_DIR, remove LOG_MAP which
# implemented specialized file-routing. The dual-logger now writes only to
# console + logs.db; any persistent file routing was removed by design.
_LOG_DIR = Path("logs")

