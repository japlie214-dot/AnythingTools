# tests/conftest.py
"""Shared pytest fixtures for AnythingTools tests.

Per pytest convention (https://docs.pytest.org/en/stable/how-to/fixtures.html):
"Puts the fixture function into a separate conftest.py file so that tests
from multiple test modules in the directory can access the fixture function."

Autouse fixtures (applied to every test automatically):
  - _disable_db_integration: sets DATABASE_INTEGRATION_ENABLED=false so no
    test accidentally writes to the real operational DB or triggers
    Snowflake sync.
  - _set_edgar_identity: sets EDGAR_IDENTITY so tests that trigger lifespan
    don't hit os.kill(SIGTERM) in init_database_layer.

Shared fixtures (opt-in via function argument):
  - tmp_db_path: a fresh SQLite DB path under pytest's tmp_path.
  - tmp_logs_db_path: a fresh logs.db path under pytest's tmp_path.
  - sqlite_conn: a real sqlite3.Connection to an isolated DB file.
  - mock_snowflake_engine: a MagicMock mimicking a Snowflake SQLAlchemy engine.
  - real_snowflake_engine: a real CloudEngine connected to Snowflake
    (requires BACKUP_CLOUD__* env vars; creates a dedicated test schema).
"""
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

@pytest.fixture(autouse=True)
def _disable_db_integration(monkeypatch):
    """Autouse: set DATABASE_INTEGRATION_ENABLED=false for all unit tests."""
    monkeypatch.setenv("DATABASE_INTEGRATION_ENABLED", "false")
    try:
        import config
        monkeypatch.setattr(config, "DATABASE_INTEGRATION_ENABLED", False)
    except ImportError:
        pass
    yield

@pytest.fixture(autouse=True)
def _set_edgar_identity(monkeypatch):
    """Autouse: set EDGAR_IDENTITY for tests that trigger lifespan."""
    monkeypatch.setenv("EDGAR_IDENTITY", "test-runner@anythingtools.local")
    try:
        import config
        monkeypatch.setattr(config, "EDGAR_IDENTITY", "test-runner@anythingtools.local")
    except ImportError:
        pass
    yield

@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Provide a fresh, isolated SQLite DB path under pytest's tmp_path."""
    return tmp_path / "test_sumanal.db"

@pytest.fixture
def tmp_logs_db_path(tmp_path: Path) -> Path:
    """Provide a fresh, isolated logs.db path under pytest's tmp_path."""
    return tmp_path / "test_logs.db"

@pytest.fixture
def sqlite_conn(tmp_db_path: Path) -> Iterator[sqlite3.Connection]:
    """Provide a real SQLite connection to an isolated DB file."""
    conn = sqlite3.connect(str(tmp_db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

@pytest.fixture
def mock_snowflake_engine():
    """Provide a MagicMock that mimics a Snowflake SQLAlchemy engine."""
    engine = MagicMock()
    conn = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)
    engine.begin = MagicMock(return_value=ctx)
    engine.dispose = MagicMock()
    return engine

@pytest.fixture(scope="session")
def snowflake_credentials_present() -> bool:
    """Check whether real Snowflake credentials are available in env."""
    return all(os.getenv(var) for var in [
        "BACKUP_CLOUD__ACCOUNT",
        "BACKUP_CLOUD__USER",
        "BACKUP_CLOUD__WAREHOUSE",
        "BACKUP_CLOUD__DATABASE",
    ]) and os.path.exists(
        os.getenv("BACKUP_CLOUD__PRIVATE_KEY_PATH", "snowflake_private_key.p8")
    )

@pytest.fixture
def real_snowflake_engine(snowflake_credentials_present):
    """Provide a real CloudEngine connected to Snowflake."""
    if not snowflake_credentials_present:
        pytest.skip(
            "Snowflake credentials not available; set BACKUP_CLOUD__* env vars "
            "to run integration tests"
        )

    from database.backup.settings import BackupSettings
    from database.backup.engine.cloud_engine import CloudEngine

    test_schema = f"BACKUP_TEST_{uuid.uuid4().hex[:8]}"
    settings = BackupSettings()
    settings.cloud.schema_name = test_schema
    engine = CloudEngine(settings.cloud, settings.sync)
    engine.startup()

    try:
        yield engine
    finally:
        try:
            with engine.engine.begin() as conn:
                from sqlalchemy import text
                conn.execute(text(f"DROP SCHEMA IF EXISTS {test_schema}"))
        except Exception:
            pass
        engine.shutdown()
