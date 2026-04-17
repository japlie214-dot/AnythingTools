# database/schema.py
import os
import sqlite3
from pathlib import Path

from database.connection import DB_PATH, DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

# Schema version constant
SCHEMA_VERSION = 2
# Allow destructive reset via environment variable
ALLOW_DESTRUCTIVE_RESET = os.getenv("SUMANAL_ALLOW_SCHEMA_RESET", "0") == "1"

INIT_SCRIPT = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    tool_name   TEXT    NOT NULL,
    args_json   TEXT    NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING','QUEUED','RUNNING','INTERRUPTED','PAUSED_FOR_HITL','COMPLETED','FAILED','ABANDONED','CANCELLING')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    result_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_session_status
    ON jobs(session_id, status);

CREATE TABLE IF NOT EXISTS job_items (
    item_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT    NOT NULL,
    step_identifier TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING','RUNNING','COMPLETED','FAILED')),
    input_data  TEXT,
    output_data TEXT,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_job_items_job_id
    ON job_items(job_id, status);

-- ── Job logs (persistent tool + runtime logs) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS job_logs (
    id           TEXT PRIMARY KEY,
    job_id       TEXT,
    tag          TEXT,
    level         TEXT,
    status_state TEXT,
    message      TEXT,
    payload_json TEXT,
    timestamp    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_job_logs_job_id
    ON job_logs(job_id, timestamp);

CREATE TABLE IF NOT EXISTS token_usage (
    id                 TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    telemetry_id       TEXT,
    provider           TEXT NOT NULL,
    model              TEXT NOT NULL,
    prompt_tokens      INTEGER NOT NULL DEFAULT 0 CHECK(prompt_tokens >= 0),
    completion_tokens  INTEGER NOT NULL DEFAULT 0 CHECK(completion_tokens >= 0),
    reasoning_tokens   INTEGER NOT NULL DEFAULT 0 CHECK(reasoning_tokens >= 0),
    total_tokens       INTEGER NOT NULL DEFAULT 0 CHECK(total_tokens >= 0),
    recorded_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_token_usage_session_recorded
    ON token_usage(session_id, recorded_at);

CREATE TABLE IF NOT EXISTS financial_metrics (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    metric_name  TEXT NOT NULL,
    metric_value REAL NOT NULL,
    metric_unit  TEXT,
    as_of        TEXT NOT NULL,
    metadata_json TEXT   NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS market_data_snapshots (
    id           TEXT PRIMARY KEY,
    symbol       TEXT NOT NULL,
    source       TEXT NOT NULL,
    snapshot_at  TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(symbol, source, snapshot_at)
);
CREATE INDEX IF NOT EXISTS idx_market_data_snapshots_symbol_snapshot_at
    ON market_data_snapshots(symbol, snapshot_at);

CREATE TABLE IF NOT EXISTS financial_formulas (
    ticker           TEXT NOT NULL,
    statement_type   TEXT NOT NULL,
    sql_query        TEXT NOT NULL,
    validation_score REAL NOT NULL DEFAULT 0.0,
    validated_at     TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, statement_type)
);
CREATE INDEX IF NOT EXISTS idx_formulas_ticker_stmt
    ON financial_formulas(ticker, statement_type, validated_at DESC);

CREATE TABLE IF NOT EXISTS calculated_metrics (
    ticker        TEXT NOT NULL,
    date          TEXT NOT NULL,
    moving_avg_3y REAL,
    std_dev_3y    REAL,
    pe_ratio      REAL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS long_term_memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT,
    agent_domain  TEXT,
    topic         TEXT NOT NULL,
    memory        TEXT NOT NULL,
    embedding     BLOB,
    type          TEXT NOT NULL DEFAULT 'Knowledge',
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_memories_agent_domain
    ON long_term_memories(agent_domain, type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_session_type
    ON long_term_memories(session_id, type, created_at DESC);

CREATE TABLE IF NOT EXISTS raw_fundamentals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    statement_type  TEXT NOT NULL,
    period_end_date TEXT NOT NULL,
    label           TEXT NOT NULL,
    concept         TEXT NOT NULL,
    value           REAL NOT NULL,
    unit            TEXT DEFAULT 'USD',
    source          TEXT DEFAULT 'SEC_EDGAR',
    extracted_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, statement_type, period_end_date, concept)
);
CREATE INDEX IF NOT EXISTS idx_raw_fundamentals_ticker_period
    ON raw_fundamentals(ticker, period_end_date);
CREATE INDEX IF NOT EXISTS idx_raw_fundamentals_concept
    ON raw_fundamentals(concept);
CREATE INDEX IF NOT EXISTS idx_fundamentals_ticker_date
    ON raw_fundamentals(ticker, statement_type, period_end_date DESC);

CREATE TABLE IF NOT EXISTS stock_prices (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   REAL,
    high   REAL,
    low    REAL,
    close  REAL,
    volume INTEGER,
    UNIQUE(ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_ticker_date
    ON stock_prices(ticker, date DESC);

-- Scraped articles knowledge base (standard table) with stored vec_rowid for JOINs
CREATE TABLE IF NOT EXISTS scraped_articles (
    id               TEXT    NOT NULL PRIMARY KEY,
    vec_rowid        INTEGER NOT NULL,
    normalized_url   TEXT    NOT NULL UNIQUE,
    url              TEXT    NOT NULL,
    title            TEXT,
    conclusion       TEXT,
    summary          TEXT,
    metadata_json    TEXT    NOT NULL DEFAULT '{}',
    embedding_status TEXT    NOT NULL DEFAULT 'PENDING'
                             CHECK(embedding_status IN ('PENDING','EMBEDDED','SKIPPED')),
    scraped_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_scraped_articles_norm_url
    ON scraped_articles(normalized_url);
CREATE INDEX IF NOT EXISTS idx_scraped_articles_status
    ON scraped_articles(embedding_status);
CREATE VIRTUAL TABLE IF NOT EXISTS scraped_articles_vec USING vec0(
    embedding float[1024]
);


CREATE TABLE IF NOT EXISTS broadcast_batches (
    batch_id              TEXT PRIMARY KEY,
    target_site           TEXT NOT NULL,
    raw_json_path         TEXT NOT NULL,
    curated_json_path     TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'PENDING'
                           CHECK(status IN (
                               'PENDING','PUBLISHING','PARTIAL','COMPLETED','FAILED'
                           )),
    posted_research_ulids TEXT NOT NULL DEFAULT '[]',
    posted_summary_ulids  TEXT NOT NULL DEFAULT '[]',
    created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_broadcast_batches_status
    ON broadcast_batches(status);
"""


def get_init_script() -> str:
    """Return the canonical INIT script with environment-aware fallbacks applied.

    This function centralizes the vec0 fallback logic so the writer thread may
    execute the returned script under the single-writer constraint.
    """
    script_to_run = INIT_SCRIPT
    try:
        from database.connection import SQLITE_VEC_AVAILABLE
    except Exception:
        SQLITE_VEC_AVAILABLE_local = False
    else:
        SQLITE_VEC_AVAILABLE_local = SQLITE_VEC_AVAILABLE

    if not SQLITE_VEC_AVAILABLE_local:
        # Replace vec0 virtual tables with basic BLOB-backed fallback tables.
        script_to_run = script_to_run.replace(
            """CREATE VIRTUAL TABLE IF NOT EXISTS scraped_articles_vec USING vec0(
    embedding float[1024]
);
""",
            """CREATE TABLE IF NOT EXISTS scraped_articles_vec (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    embedding BLOB
);
""",
        )

        script_to_run = script_to_run.replace(
            """CREATE VIRTUAL TABLE IF NOT EXISTS long_term_memories_vec USING vec0(
    embedding float[1024]
);
""",
            """CREATE TABLE IF NOT EXISTS long_term_memories_vec (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    embedding BLOB
);
""",
        )

        script_to_run = script_to_run.replace(
            """CREATE VIRTUAL TABLE IF NOT EXISTS pdf_parsed_pages_vec USING vec0(
    embedding float[1024]
);
""",
            """CREATE TABLE IF NOT EXISTS pdf_parsed_pages_vec (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    embedding BLOB
);
""",
        )

    return script_to_run


def _remove_db_files() -> None:
    """Delete the primary SQLite file and its -wal and -shm side‑car files."""
    for path in (
        DB_PATH,
        DB_PATH.with_name(f"{DB_PATH.name}-wal"),
        DB_PATH.with_name(f"{DB_PATH.name}-shm"),
    ):
        if path.exists():
            path.unlink()


def init_db() -> None:
    """Initialize (or migrate) the database schema.

    If the existing schema version differs from ``SCHEMA_VERSION`` a destructive
    reset is performed only when ``ALLOW_DESTRUCTIVE_RESET`` is true.
    """
    conn = DatabaseManager.create_write_connection()
    try:
        cur = conn.cursor()
        try:
            current_v = cur.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError:
            current_v = 0
        
        # ── Selective destructive reset for legacy versions ────────────────
        if current_v != SCHEMA_VERSION and ALLOW_DESTRUCTIVE_RESET:
            log.dual_log(
                tag="DB:Schema",
                message=f"Performing selective destructive reset to v{SCHEMA_VERSION}.",
                level="WARNING",
                payload={"current_version": current_v, "target_version": SCHEMA_VERSION},
            )
            # Step 1: Drop all legacy tables atomically, including chat_messages to apply new schema.
            tables_to_drop = [
                'sessions', 'execution_ledger', 'active_chat_state', 'tool_telemetry', 'grouped_formulas',
                'job_cache', 'chat_history', 'token_usage', 'financial_metrics',
                'long_term_memories', 'long_term_memories_vec', 'market_data_snapshots',
                'financial_formulas', 'calculated_metrics', 'raw_fundamentals', 'stock_prices',
                'pdf_parsed_pages', 'pdf_parsed_pages_vec', 'browser_macros', 'ai_skills',
                'scraped_articles', 'scraped_articles_vec', 'chat_messages'
            ]
            for _t in tables_to_drop:
                conn.execute(f"DROP TABLE IF EXISTS {_t}")
            conn.commit()

        # ── Defensive column migration: event_id ─────────────────────────────
        # c[1] is the column name field in PRAGMA table_info output.
        # Positional access is used because row_factory may not be sqlite3.Row here.
        # NOTE: The chat_messages table has been replaced by execution_ledger,
        # so this entire block is intentionally commented out / removed.
        # If new columns are needed on execution_ledger, we must create a new migration
        # step instead.
        try:
            pass
        except Exception:
            pass

        # Prepare final script using helper and execute it (single-writer callers should
        # use `get_init_script()` and have the writer exec it; this path is kept for
        # compatibility when init_db() is invoked directly).
        script_to_run = get_init_script()
        conn.executescript(script_to_run)


        # ── Set schema version ──────────────────────────────────────────────
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
