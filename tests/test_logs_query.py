# tests/test_logs_query.py
import sqlite3
import json
import os
import pytest
from pathlib import Path
from typing import Any
from scripts.logs_query import (
    main, 
    _resolve_db_path, 
    _parse_since, 
    DEFAULT_LOGS_DB_PATH
)

@pytest.fixture
def mock_logs_db(tmp_path):
    """Create a temporary logs.db with sample data."""
    db_file = tmp_path / "logs.db"
    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE logs (
            id TEXT PRIMARY KEY,
            timestamp TEXT,
            level TEXT,
            tag TEXT,
            job_id TEXT,
            message TEXT,
            payload_json TEXT,
            error_json TEXT,
            status_state TEXT,
            event_id TEXT
        )
    """)
    
    logs = [
        ("L1", "2026-06-18T08:00:00Z", "INFO", "System:Start", "JOB1", "Server started", '{"version": "1.0"}', None, "STARTING", "E1"),
        ("L2", "2026-06-18T08:05:00Z", "ERROR", "Backup:Cloud", "JOB1", "Cloud sync failed", '{"table": "articles"}', '{"error": "timeout"}', "FAILED", "E2"),
        ("L3", "2026-06-18T08:10:00Z", "WARNING", "DB:Writer", "JOB2", "Slow write detected", '{"duration": 500}', None, "STALLED", "E3"),
        ("L4", "2026-06-18T08:15:00Z", "DEBUG", "Internal:Cache", "JOB2", "Cache hit", '{"key": "user_1"}', None, None, None),
    ]
    
    conn.executemany(
        "INSERT INTO logs VALUES (?,?,?,?,?,?,?,?,?,?)", 
        logs
    )
    conn.commit()
    conn.close()
    
    # Patch the environment so the script finds this DB
    return db_file

def test_parse_since():
    """Test time parsing logic."""
    # ISO 8601
    assert _parse_since("2026-06-18T08:00:00Z") == "2026-06-18T08:00:00+00:00"
    # Relative
    res = _parse_since("1h")
    assert res is not None
    assert len(res) > 10

def test_recent_query(mock_logs_db, capsys):
    """Test 'recent' subcommand."""
    os.environ["LOGS_DB_PATH"] = str(mock_logs_db)
    
    # Test default markdown
    main(["recent", "--limit", "10"])
    captured = capsys.readouterr().out
    assert "Server started" in captured
    assert "Cloud sync failed" in captured
    
    # Test JSON output
    capsys.readouterr() # clear
    main(["recent", "--json"])
    captured_json = capsys.readouterr().out
    data = json.loads(captured_json)
    assert isinstance(data, list)
    assert data[0]["id"] == "L4" # Most recent first

def test_search_query(mock_logs_db, capsys):
    """Test 'search' subcommand including payload search."""
    os.environ["LOGS_DB_PATH"] = str(mock_logs_db)
    
    # Search message only
    main(["search", "Server started"])
    assert "L1" in capsys.readouterr().out
    
    # Search payload only (should fail without --search-payload)
    capsys.readouterr()
    main(["search", "version"])
    assert "L1" not in capsys.readouterr().out
    
    # Search payload with flag
    capsys.readouterr()
    main(["search", "version", "--search-payload"])
    assert "L1" in capsys.readouterr().out

def test_by_tag_query(mock_logs_db, capsys):
    """Test 'by-tag' subcommand."""
    os.environ["LOGS_DB_PATH"] = str(mock_logs_db)
    
    main(["by-tag", "Backup:Cloud"])
    captured = capsys.readouterr().out
    assert "L2" in captured
    assert "L1" not in captured

def test_show_detail(mock_logs_db, capsys):
    """Test 'show' subcommand."""
    os.environ["LOGS_DB_PATH"] = str(mock_logs_db)
    
    main(["show", "L2"])
    captured = capsys.readouterr().out
    assert "## Log Entry `L2`" in captured
    assert "Cloud sync failed" in captured
    assert "timeout" in captured

def test_stats_query(mock_logs_db, capsys):
    """Test 'stats' subcommand."""
    os.environ["LOGS_DB_PATH"] = str(mock_logs_db)
    
    main(["stats"])
    captured = capsys.readouterr().out
    assert "Total entries: 4" in captured
    assert "ERROR" in captured
    assert "Backup:Cloud" in captured
