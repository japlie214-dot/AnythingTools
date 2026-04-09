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

# Optional sqlite_vec helper; fail gracefully when it's not present.
try:
    import sqlite_vec  # type: ignore
    SQLITE_VEC_AVAILABLE = True
except Exception:
    sqlite_vec = None  # type: ignore
    SQLITE_VEC_AVAILABLE = False

# Path to the SQLite database file. Adjust as needed.
DB_PATH = Path("data") / "sumanal.db"

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

            # Try to load sqlite_vec if available; fail gracefully if not.
            if SQLITE_VEC_AVAILABLE:
                try:
                    conn.enable_load_extension(True)
                    sqlite_vec.load(conn)
                except Exception:
                    # Extension failed to load in this environment - continue without it.
                    pass

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

        if SQLITE_VEC_AVAILABLE:
            try:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
            except Exception:
                # Ignore extension failures for write connection as well.
                pass

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
