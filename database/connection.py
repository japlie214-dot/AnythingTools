# database/connection.py
"""Database connection helpers for AnythingTools.

This module intentionally makes the sqlite_vec extension optional so the
project can initialize the schema and run in environments where the
binary extension is not installed (development machines, CI, etc.).

If sqlite_vec is available, it will be loaded when connections are
created. If not available, code will continue to run with vector
functionality disabled.
"""
from pathlib import Path
import sqlite3
import threading
import sys

# Optional sqlite_vec helper; fail gracefully when it's not present.
try:
    import sqlite_vec  # type: ignore
    SQLITE_VEC_AVAILABLE = True
except Exception:
    sqlite_vec = None  # type: ignore
    SQLITE_VEC_AVAILABLE = False

_VEC_PERMANENTLY_FAILED = False
_VEC_LOAD_ANNOUNCED = False

# vec0 startup write-probes removed per PLAN-01: use read-only probes and nuke mechanism instead.
# See database/management/reconciler.py for read-only verification and nuke implementation.

def _attempt_vec_load(conn: sqlite3.Connection) -> bool:
    global _VEC_PERMANENTLY_FAILED, _VEC_LOAD_ANNOUNCED
    if not SQLITE_VEC_AVAILABLE:
        if not _VEC_LOAD_ANNOUNCED:
            _VEC_LOAD_ANNOUNCED = True
            try:
                from utils.logger import get_dual_logger
                get_dual_logger(__name__).dual_log(tag="Database:Vector:LoadError", message="sqlite-vec Python package not installed. Vector search disabled.", level="WARNING", payload={"available": False})
            except Exception:
                sys.stderr.write("[WARN] sqlite-vec Python package not installed. Vector search disabled.\n")
        return False
    if _VEC_PERMANENTLY_FAILED:
        return False

    try:
        version_row = conn.execute("SELECT vec_version()").fetchone()
        if version_row:
            return True
    except Exception:
        pass

    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as exc:
        if not _VEC_LOAD_ANNOUNCED:
            _VEC_LOAD_ANNOUNCED = True
            try:
                from utils.logger import get_dual_logger
                get_dual_logger(__name__).dual_log(tag="Database:Vector:LoadError", message="sqlite-vec extension failed to load. Falling back to FTS5.", level="CRITICAL", payload={"error": str(exc), "available": False})
            except Exception:
                sys.stderr.write(f"[CRITICAL] sqlite-vec extension failed to load: {exc}\n")
        _VEC_PERMANENTLY_FAILED = True
        return False

    try:
        version_row = conn.execute("SELECT vec_version()").fetchone()
        vec_ver = version_row[0] if version_row else "unknown"
        if not _VEC_LOAD_ANNOUNCED:
            _VEC_LOAD_ANNOUNCED = True
            try:
                from utils.logger import get_dual_logger
                get_dual_logger(__name__).dual_log(tag="Database:Vector:LoadSuccess", message="sqlite-vec loaded successfully", level="INFO", payload={"version": vec_ver, "available": True})
            except Exception:
                pass
    except Exception as exc:
        if not _VEC_LOAD_ANNOUNCED:
            _VEC_LOAD_ANNOUNCED = True
            try:
                from utils.logger import get_dual_logger
                get_dual_logger(__name__).dual_log(tag="Database:Vector:LoadError", message="sqlite-vec loaded but vec_version() query failed", level="CRITICAL", payload={"error": str(exc)})
            except Exception:
                sys.stderr.write(f"[CRITICAL] sqlite-vec loaded but vec_version() query failed: {exc}\n")
        _VEC_PERMANENTLY_FAILED = True
        return False
    return True

# Path to the SQLite database files. Adjust as needed.
DB_PATH = Path("data") / "sumanal.db"
LOGS_DB_PATH = Path("data") / "logs.db"

# Connection configuration constants
READ_TIMEOUT_SECONDS = 30.0
BUSY_TIMEOUT_MS = 5000


class DatabaseManager:
    _local = threading.local()
    _last_seen_generation = threading.local()

    @classmethod
    def get_read_connection(cls) -> sqlite3.Connection:
        """Thread-local, query-only connection for read operations."""
        # Import writer generation at call-time to avoid circular import at module import.
        from database.writer import get_write_generation

        current_gen = get_write_generation()

        # If a connection exists but was created before the latest write generation,
        # force a refresh (close + recreate) so that subsequent reads observe the latest WAL commits.
        if hasattr(cls._local, "conn") and cls._local.conn is not None:
            last_seen = getattr(cls._last_seen_generation, "gen", -1)
            if last_seen < current_gen:
                try:
                    cls._local.conn.close()
                except Exception:
                    pass
                cls._local.conn = None

        if not hasattr(cls._local, "conn") or cls._local.conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(DB_PATH), timeout=READ_TIMEOUT_SECONDS, check_same_thread=True
            )

            _attempt_vec_load(conn)

            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA query_only = ON")  # Enforce read-only mode must be LAST
            cls._local.conn = conn

        # Record that this thread now observes the current write generation.
        cls._last_seen_generation.gen = current_gen
        return cls._local.conn

    @classmethod
    def get_connection(cls) -> sqlite3.Connection:
        """Alias for read‑only connections used throughout the codebase."""
        return cls.get_read_connection()

    @staticmethod
    def create_write_connection() -> sqlite3.Connection:
        """Dedicated connection for schema initialization and the writer thread."""
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(DB_PATH), timeout=READ_TIMEOUT_SECONDS, check_same_thread=True
        )

        _attempt_vec_load(conn)

        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")

        return conn

    @classmethod
    def close_read_connection(cls) -> None:
        conn = getattr(cls._local, "conn", None)
        if conn is not None:
            conn.close()
            cls._local.conn = None


class LogsDatabaseManager:
    """Manager for the independent logs database with high-throughput pragmas."""
    _local = threading.local()
    _last_seen_generation = threading.local()

    @classmethod
    def get_read_connection(cls) -> sqlite3.Connection:
        """Thread-local, query-only connection for read operations on logs.db."""
        # Import writer generation at call-time to avoid circular import.
        from database.logs_writer import get_logs_write_generation
        
        current_gen = get_logs_write_generation()

        # Refresh connection if a newer write generation exists
        if hasattr(cls._local, "conn") and cls._local.conn is not None:
            last_seen = getattr(cls._last_seen_generation, "gen", -1)
            if last_seen < current_gen:
                try:
                    cls._local.conn.close()
                except Exception:
                    pass
                cls._local.conn = None

        if not hasattr(cls._local, "conn") or cls._local.conn is None:
            LOGS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(LOGS_DB_PATH), timeout=READ_TIMEOUT_SECONDS, check_same_thread=True
            )
            conn.row_factory = sqlite3.Row
            # High-throughput optimizations for logs
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = OFF")
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA query_only = ON")
            cls._local.conn = conn

        cls._last_seen_generation.gen = current_gen
        return cls._local.conn

    @classmethod
    def get_connection(cls) -> sqlite3.Connection:
        """Alias for read-only connections used throughout the codebase."""
        return cls.get_read_connection()

    @staticmethod
    def create_write_connection() -> sqlite3.Connection:
        """Dedicated connection for the logs writer thread."""
        LOGS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(LOGS_DB_PATH), timeout=READ_TIMEOUT_SECONDS, check_same_thread=True
        )
        conn.row_factory = sqlite3.Row
        # High-throughput optimizations for logs
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return conn

    @classmethod
    def close_read_connection(cls) -> None:
        conn = getattr(cls._local, "conn", None)
        if conn is not None:
            conn.close()
            cls._local.conn = None
