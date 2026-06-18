# tests/test_backup.py
import pytest
from unittest.mock import MagicMock
from database.backup.resilience.session_recovery import (
    _is_session_expired,
    with_session_recovery,
    register_session_recovery,
)


class FakeProgrammingError(Exception):
    """Mimics snowflake.connector.errors.ProgrammingError for testing.

    Per the Snowflake Python Connector API:
    https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-api
    "The Snowflake Connector for Python provides the attributes msg, errno,
    sqlstate, sfqid and raw_msg."
    """

    def __init__(self, errno=None, sqlstate=None, msg=""):
        self.errno = errno
        self.sqlstate = sqlstate
        self.msg = msg
        self.sfqid = None
        super().__init__(f"{errno or 0:06d} ({sqlstate or '00000'}): {msg}")


class TestSessionExpiredDetection:
    """Verify _is_session_expired correctly identifies Snowflake session-gone
    errors while NOT matching other errors (e.g. 100090 duplicate-row MERGE).
    """

    def test_390111_errno_matched(self):
        exc = FakeProgrammingError(
            errno=390111, sqlstate="08003", msg="Session no longer exists"
        )
        assert _is_session_expired(exc) is True

    def test_390114_errno_matched(self):
        """390114 is the 'Authentication token has expired' variant."""
        exc = FakeProgrammingError(errno=390114, sqlstate="08003")
        assert _is_session_expired(exc) is True

    def test_100090_not_matched(self):
        """The duplicate-row MERGE error must NOT be treated as session-expired.
        """
        exc = FakeProgrammingError(
            errno=100090, sqlstate="42P18", msg="Duplicate row detected"
        )
        assert _is_session_expired(exc) is False

    def test_sqlstate_08003_matched(self):
        """Defensive: if errno is missing but sqlstate is 08003, still match."""
        exc = FakeProgrammingError(errno=None, sqlstate="08003")
        assert _is_session_expired(exc) is True

    def test_message_substring_fallback(self):
        """Defensive: if errno and sqlstate are missing but the message contains
        the canonical 390111 string, still match."""
        exc = FakeProgrammingError(
            errno=None, sqlstate=None,
            msg="Session no longer exists. New login required to access this service."
        )
        assert _is_session_expired(exc) is True

    def test_non_snowflake_exception_not_matched(self):
        """Generic Python exceptions must not be treated as session-expired."""
        assert _is_session_expired(ValueError("foo")) is False
        assert _is_session_expired(RuntimeError("bar")) is False


class TestWithSessionRecovery:
    """Verify the with_session_recovery decorator retries on 390111 and
    re-raises on second failure or non-390111 errors.
    """

    def test_retries_on_390111_and_succeeds(self):
        """First call raises 390111; second call succeeds."""
        engine = MagicMock()
        log = MagicMock()
        call_count = {"n": 0}

        def fn():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise FakeProgrammingError(errno=390111, sqlstate="08003")
            return "ok"

        recovered_fn = with_session_recovery(fn, engine=engine, log=log, tag="test", max_retries=1)

        assert recovered_fn() == "ok"
        assert call_count["n"] == 2
        engine.dispose.assert_called_once()

    def test_no_retry_on_100090(self):
        """100090 (duplicate-row MERGE error) must NOT be retried."""
        engine = MagicMock()
        log = MagicMock()

        def fn():
            raise FakeProgrammingError(
                errno=100090, sqlstate="42P18", msg="Duplicate row detected"
            )

        recovered_fn = with_session_recovery(fn, engine=engine, log=log, tag="test", max_retries=1)

        with pytest.raises(FakeProgrammingError):
            recovered_fn()
        engine.dispose.assert_not_called()

    def test_re_raise_on_second_390111(self):
        """If the retry also raises 390111, re-raise."""
        engine = MagicMock()
        log = MagicMock()

        def fn():
            raise FakeProgrammingError(errno=390111, sqlstate="08003")

        recovered_fn = with_session_recovery(fn, engine=engine, log=log, tag="test", max_retries=1)

        with pytest.raises(FakeProgrammingError):
            recovered_fn()
        assert engine.dispose.call_count == 1

    def test_max_retries_zero_disables_retry(self):
        """If max_retries=0, the decorator is a passthrough on 390111."""
        engine = MagicMock()
        log = MagicMock()

        def fn():
            raise FakeProgrammingError(errno=390111, sqlstate="08003")

        recovered_fn = with_session_recovery(fn, engine=engine, log=log, tag="test", max_retries=0)

        with pytest.raises(FakeProgrammingError):
            recovered_fn()
        engine.dispose.assert_not_called()

    def test_non_snowflake_exception_passes_through(self):
        """Generic exceptions must propagate unchanged."""
        engine = MagicMock()
        log = MagicMock()

        def fn():
            raise RuntimeError("some other error")

        recovered_fn = with_session_recovery(fn, engine=engine, log=log, tag="test", max_retries=1)

        with pytest.raises(RuntimeError):
            recovered_fn()
        engine.dispose.assert_not_called()


class TestCompositePKDetection:
    """Verify _detect_pk_columns correctly returns a list for composite PKs
    and a string for single PKs, in declaration order.
    """

    def test_detects_composite_pk_in_declaration_order(self, tmp_path):
        import sqlite3
        db = tmp_path / "test.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE sf_quarterly_facts (
                ticker TEXT NOT NULL,
                statement_type TEXT NOT NULL,
                concept TEXT NOT NULL,
                quarter TEXT NOT NULL,
                content_hash TEXT,
                PRIMARY KEY (ticker, statement_type, concept, quarter)
            );
        """)
        from database.backup.engine.sync_operations import _detect_pk_columns
        pk_col, has_hash = _detect_pk_columns(conn, "sf_quarterly_facts")
        assert pk_col == ["ticker", "statement_type", "concept", "quarter"]
        assert has_hash is True
        conn.close()

    def test_detects_single_pk_returns_string(self, tmp_path):
        import sqlite3
        db = tmp_path / "test.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE scraped_articles (
                id TEXT PRIMARY KEY,
                content_hash TEXT
            );
        """)
        from database.backup.engine.sync_operations import _detect_pk_columns
        pk_col, has_hash = _detect_pk_columns(conn, "scraped_articles")
        assert pk_col == "id"
        assert has_hash is True
        conn.close()

    def test_detects_no_pk_defaults_to_id(self, tmp_path):
        import sqlite3
        db = tmp_path / "test.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE no_pk (
                some_col TEXT,
                content_hash TEXT
            );
        """)
        from database.backup.engine.sync_operations import _detect_pk_columns
        pk_col, has_hash = _detect_pk_columns(conn, "no_pk")
        assert pk_col == "id"
        assert has_hash is True
        conn.close()

    def test_detects_no_content_hash(self, tmp_path):
        import sqlite3
        db = tmp_path / "test.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE no_hash (
                id TEXT PRIMARY KEY,
                data TEXT
            );
        """)
        from database.backup.engine.sync_operations import _detect_pk_columns
        pk_col, has_hash = _detect_pk_columns(conn, "no_hash")
        assert pk_col == "id"
        assert has_hash is False
        conn.close()

    def test_compute_chunk_size_respects_param_limit(self):
        from database.backup.engine.sync_operations import (
            _compute_chunk_size,
            SQLITE_HOST_PARAM_LIMIT,
            MAX_CHUNK_SIZE,
        )
        assert _compute_chunk_size(["id"]) == MAX_CHUNK_SIZE
        assert _compute_chunk_size(["a", "b"]) == SQLITE_HOST_PARAM_LIMIT // 2
        assert _compute_chunk_size(["a", "b", "c", "d"]) == SQLITE_HOST_PARAM_LIMIT // 4
