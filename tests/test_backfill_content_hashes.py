# tests/test_backfill_content_hashes.py
"""Verify the content_hash backfill logic used by the maintenance job
scripts/backfill_content_hashes.py.

The maintenance job reuses database.schemas.column_defaults._fill_content_hash
(the SAME function used by the schema migration system). These tests verify
that function directly against an in-memory SQLite DB.
"""
import hashlib
import sqlite3
from pathlib import Path

from database.schemas.column_defaults import _fill_content_hash


def _make_test_db(path: Path) -> sqlite3.Connection:
    """Create a test DB with one table that has a content_hash column."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE scraped_articles (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            content_hash TEXT NOT NULL DEFAULT '',
            updated_at TEXT
        );
        INSERT INTO scraped_articles (id, title, body, updated_at) VALUES
            ('a1', 'Title 1', 'Body 1', '2024-01-01T00:00:00Z'),
            ('a2', 'Title 2', 'Body 2', '2024-01-02T00:00:00Z'),
            ('a3', 'Title 3', 'Body 3', '2024-01-03T00:00:00Z');
    """)
    conn.commit()
    return conn


def test_fill_content_hash_backfills_empty_rows(tmp_path):
    """_fill_content_hash must compute SHA256 of checksum columns for
    rows where content_hash is empty."""
    db_path = tmp_path / "test.db"
    conn = _make_test_db(db_path)

    # All 3 rows have empty content_hash initially.
    rows = conn.execute("SELECT id, content_hash FROM scraped_articles").fetchall()
    assert all(r[1] == "" for r in rows)

    # Run the filler (registered for scraped_articles in column_defaults.py).
    filled = _fill_content_hash(conn, "scraped_articles", "content_hash")
    assert filled == 3

    # Verify the hashes are non-empty and deterministic.
    rows = conn.execute("SELECT id, content_hash FROM scraped_articles").fetchall()
    for r in rows:
        assert r[1] != ""
        assert len(r[1]) == 64  # SHA256 hex digest length

    conn.close()


def test_fill_content_hash_is_idempotent(tmp_path):
    """Running the filler twice must not change existing hashes."""
    db_path = tmp_path / "test.db"
    conn = _make_test_db(db_path)

    _fill_content_hash(conn, "scraped_articles", "content_hash")
    hashes_after_first = conn.execute(
        "SELECT id, content_hash FROM scraped_articles ORDER BY id"
    ).fetchall()

    _fill_content_hash(conn, "scraped_articles", "content_hash")
    hashes_after_second = conn.execute(
        "SELECT id, content_hash FROM scraped_articles ORDER BY id"
    ).fetchall()

    assert hashes_after_first == hashes_after_second
    conn.close()


def test_fill_content_hash_skips_non_empty_rows(tmp_path):
    """Rows with a non-empty content_hash must be untouched."""
    db_path = tmp_path / "test.db"
    conn = _make_test_db(db_path)

    # Pre-set a hash on one row.
    conn.execute("UPDATE scraped_articles SET content_hash = 'preset' WHERE id = 'a1'")
    conn.commit()

    filled = _fill_content_hash(conn, "scraped_articles", "content_hash")
    assert filled == 2  # only the 2 empty rows

    preset = conn.execute(
        "SELECT content_hash FROM scraped_articles WHERE id = 'a1'"
    ).fetchone()[0]
    assert preset == "preset"

    conn.close()
