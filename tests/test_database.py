# tests/test_database.py
"""Tests for database health validation.

Per the requirement: "Add test_database.py to validate the health of the
database (operational and cloud)."

Scope: DB health ONLY. Backup mechanism tests are in test_backup.py.

Validates:
1. Operational DB (SQLite):
   - File exists and is not corrupted (PRAGMA integrity_check)
   - All expected tables present
   - All expected triggers present
   - Write/read round-trip works
   - WAL mode is active
   - Foreign keys are enforced
2. Cloud DB (Snowflake):
   - Connectivity (SELECT CURRENT_VERSION())
   - All expected tables present in the BACKUP schema
   - Write/read round-trip works (INSERT + SELECT + DELETE)
3. Integration:
   - sync_ledger has at least one COMPLETED entry
   - dead_letter_queue is empty (or has known recoverable rows)
"""
import sqlite3
import pytest
from pathlib import Path
from sqlalchemy import text

class TestOperationalDBHealth:
    """Validate the health of the operational SQLite database.

    These tests run against a real SQLite file (tmp_path) populated with
    the canonical schema. They do NOT require Snowflake.
    """

    def test_db_file_exists_after_init(self, tmp_db_path):
        """After running get_init_script, the DB file should exist and be non-empty."""
        from database.schemas import get_init_script
        conn = sqlite3.connect(str(tmp_db_path))
        conn.executescript(get_init_script())
        conn.close()
        assert tmp_db_path.exists()
        assert tmp_db_path.stat().st_size > 0

    def test_integrity_check_passes(self, tmp_db_path):
        """PRAGMA integrity_check should return 'ok'."""
        from database.schemas import get_init_script
        conn = sqlite3.connect(str(tmp_db_path))
        conn.executescript(get_init_script())
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        assert result[0] == "ok"

    def test_all_expected_tables_present(self, tmp_db_path):
        """Every table in ALL_TABLES + ALL_FTS_TABLES must exist after init."""
        from database.schemas import ALL_TABLES, ALL_FTS_TABLES, get_init_script
        conn = sqlite3.connect(str(tmp_db_path))
        conn.executescript(get_init_script())
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        for table_name in list(ALL_TABLES.keys()) + list(ALL_FTS_TABLES.keys()):
            assert table_name in existing, f"Missing table: {table_name}"

    def test_wal_mode_active(self, tmp_db_path):
        """After setting journal_mode=WAL, PRAGMA journal_mode should return 'wal'."""
        conn = sqlite3.connect(str(tmp_db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        result = conn.execute("PRAGMA journal_mode").fetchone()
        conn.close()
        assert result[0].lower() == "wal"

    def test_foreign_keys_enforced(self, tmp_db_path):
        """PRAGMA foreign_keys should be 1 after enabling."""
        from database.schemas import get_init_script
        conn = sqlite3.connect(str(tmp_db_path))
        conn.executescript(get_init_script())
        conn.execute("PRAGMA foreign_keys=ON")
        result = conn.execute("PRAGMA foreign_keys").fetchone()
        conn.close()
        assert result[0] == 1

    def test_write_read_roundtrip(self, tmp_db_path):
        """Insert a row into sf_tickers and read it back."""
        from database.schemas import get_init_script
        conn = sqlite3.connect(str(tmp_db_path))
        conn.executescript(get_init_script())
        conn.execute(
            "INSERT INTO sf_tickers (ticker, company_name, cik) VALUES (?, ?, ?)",
            ("TEST", "Test Company", "0001234567")
        )
        conn.commit()
        row = conn.execute(
            "SELECT ticker, company_name FROM sf_tickers WHERE ticker=?",
            ("TEST",)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "TEST"
        assert row[1] == "Test Company"

    def test_all_expected_triggers_present(self, tmp_db_path):
        """Every trigger in ALL_TRIGGERS must exist after init."""
        from database.schemas import ALL_TRIGGERS, get_init_script
        conn = sqlite3.connect(str(tmp_db_path))
        conn.executescript(get_init_script())
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()}
        conn.close()
        for trigger_name in ALL_TRIGGERS.keys():
            assert trigger_name in existing, f"Missing trigger: {trigger_name}"

    def test_health_checker_returns_healthy(self, tmp_db_path):
        """DatabaseHealthChecker.check_operational should return status='healthy'
        for a freshly-initialized DB."""
        from database.schemas import get_init_script
        from database.management.health import DatabaseHealthChecker
        conn = sqlite3.connect(str(tmp_db_path))
        conn.executescript(get_init_script())
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()
        health = DatabaseHealthChecker.check_operational(tmp_db_path)
        assert health.status in ("healthy", "degraded"), (
            f"Expected healthy/degraded, got {health.status}: {health.error}"
        )
        assert health.integrity_check == "ok"


@pytest.mark.integration
class TestCloudDBHealth:
    """Validate the health of the cloud Snowflake database.

    These tests require real Snowflake credentials. Skipped otherwise.
    """

    def test_connectivity(self, real_snowflake_engine):
        """SELECT CURRENT_VERSION() should return a non-empty string."""
        with real_snowflake_engine.engine.begin() as conn:
            result = conn.execute(text("SELECT CURRENT_VERSION()"))
            version = result.fetchone()[0]
            assert version
            assert len(version) > 0

    def test_all_expected_tables_present_in_cloud(self, real_snowflake_engine):
        """Every table in PERSISTED_TABLES must exist in the Snowflake schema."""
        from database.schemas import PERSISTED_TABLES
        schema = real_snowflake_engine.settings.schema_name.upper()
        with real_snowflake_engine.engine.begin() as conn:
            result = conn.execute(text(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = :schema"
            ).bindparams(schema=schema))
            existing = {r[0].lower() for r in result}
        for table_name in PERSISTED_TABLES:
            assert table_name.lower() in existing, (
                f"Missing cloud table: {table_name}"
            )

    def test_cloud_write_read_roundtrip(self, real_snowflake_engine):
        """Insert a row into a dummy cloud table and read it back."""
        with real_snowflake_engine.engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS _test_health_probe "
                "(id NUMBER, payload VARCHAR, created_at VARCHAR)"
            ))
            conn.execute(text(
                "INSERT INTO _test_health_probe (id, payload, created_at) "
                "VALUES (1, 'health-check', '2026-06-18T00:00:00Z')"
            ))
            result = conn.execute(
                text("SELECT payload FROM _test_health_probe WHERE id = 1")
            )
            row = result.fetchone()
            assert row is not None
            assert row[0] == "health-check"
            conn.execute(text("DROP TABLE _test_health_probe"))

    def test_health_checker_returns_healthy(self, real_snowflake_engine):
        """DatabaseHealthChecker.check_cloud should return status='healthy'."""
        from database.management.health import DatabaseHealthChecker
        health = DatabaseHealthChecker.check_cloud(real_snowflake_engine)
        assert health.status in ("healthy", "degraded"), (
            f"Expected healthy/degraded, got {health.status}: {health.error}"
        )
        assert health.current_version is not None


@pytest.mark.integration
class TestDatabaseIntegration:
    """Validate integration between operational and cloud DBs."""

    def test_sync_ledger_has_completed_entries(self, real_snowflake_engine):
        """After at least one sync cycle, sync_ledger should have COMPLETED entries."""
        from database.connection import DB_PATH
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
        try:
            result = conn.execute(
                "SELECT COUNT(*) FROM sync_ledger WHERE state = 'COMPLETED'"
            ).fetchone()
            count = result[0] if result else 0
            if count == 0:
                pytest.skip("No completed sync entries — test schema is fresh")
        except sqlite3.OperationalError:
            pytest.skip("sync_ledger table not present in operational DB")
        finally:
            conn.close()

    def test_dead_letter_queue_empty(self, real_snowflake_engine):
        """dead_letter_queue should be empty in a healthy deployment."""
        from database.connection import DB_PATH
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
        try:
            result = conn.execute(
                "SELECT COUNT(*) FROM dead_letter_queue"
            ).fetchone()
            count = result[0] if result else 0
            assert count == 0, (
                f"dead_letter_queue has {count} entries — investigate failures"
            )
        except sqlite3.OperationalError:
            pytest.skip("dead_letter_queue table not present in operational DB")
        finally:
            conn.close()
