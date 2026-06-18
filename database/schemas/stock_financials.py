# database?schemas/stock_financials.py
TABLES = {
    "sf_tickers": """CREATE TABLE IF NOT EXISTS sf_tickers (
        ticker TEXT PRIMARY KEY,
        company_name TEXT NOT NULL DEFAULT '',
        cik INTEGER NOT NULL DEFAULT 0,
        fiscal_year_end_month INTEGER NOT NULL DEFAULT 12,
        fiscal_year_end_date TEXT NOT NULL DEFAULT '',
        latest_quarter TEXT NOT NULL DEFAULT '',
        latest_extraction_at TEXT NOT NULL DEFAULT '',
        content_hash TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_sf_tickers_cik ON sf_tickers(cik);""",

    "sf_quarterly_facts": """CREATE TABLE IF NOT EXISTS sf_quarterly_facts (
        ticker TEXT NOT NULL,
        statement_type TEXT NOT NULL,
        concept TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '',
        quarter TEXT NOT NULL,
        period_end TEXT NOT NULL DEFAULT '',
        fiscal_period TEXT NOT NULL DEFAULT '',
        fiscal_year INTEGER NOT NULL DEFAULT 0,
        numeric_value TEXT NOT NULL DEFAULT '',
        unit TEXT NOT NULL DEFAULT 'USD',
        period_type TEXT NOT NULL DEFAULT 'duration',
        depth INTEGER NOT NULL DEFAULT 0,
        is_total INTEGER NOT NULL DEFAULT 0,
        concept_order INTEGER NOT NULL DEFAULT 0,
        content_hash TEXT NOT NULL DEFAULT '',
        extracted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (ticker, statement_type, concept, quarter),
        -- Foreign key with cascade delete. Per https://www.sqlite.org/foreignkeys.html,
        -- enforcement requires PRAGMA foreign_keys = ON, which is already set
        -- in database/connection.py:139,166. Note: SQLite cannot ALTER TABLE
        -- to add FKs to an existing table (https://www.sqlite.org/lang_altertable.html);
        -- this clause only applies on fresh DBs. Existing DBs need a refresh=true
        -- extract to clean orphaned rows.
        FOREIGN KEY (ticker) REFERENCES sf_tickers(ticker) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_sf_facts_ticker_stmt ON sf_quarterly_facts(ticker, statement_type);
    CREATE INDEX IF NOT EXISTS idx_sf_facts_ticker_quarter ON sf_quarterly_facts(ticker, quarter, statement_type);
    CREATE INDEX IF NOT EXISTS idx_sf_facts_concept ON sf_quarterly_facts(concept, ticker);"""
}

# Re-export from the centralized override registry for backward compatibility.
# New overrides should be added to database/schemas/_snowflake_overrides.py
# rather than here, so that non-stock-financials tables (e.g. vector tables
# like scraped_articles_vec_backup) can be registered without coupling to
# this module.
from database.schemas._snowflake_overrides import SNOWFLAKE_COLUMN_OVERRIDES  # noqa: F401
