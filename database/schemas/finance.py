# database/schemas/finance.py

TABLES = {
    "financial_metrics": """
        CREATE TABLE IF NOT EXISTS financial_metrics (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            metric_value REAL NOT NULL,
            metric_unit TEXT,
            as_of TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
    """,
    "market_data_snapshots": """
        CREATE TABLE IF NOT EXISTS market_data_snapshots (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            source TEXT NOT NULL,
            snapshot_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(symbol, source, snapshot_at)
        );
        CREATE INDEX IF NOT EXISTS idx_market_data_snapshots_symbol_snapshot_at ON market_data_snapshots(symbol, snapshot_at);
    """,
    "financial_formulas": """
        CREATE TABLE IF NOT EXISTS financial_formulas (
            ticker TEXT NOT NULL,
            statement_type TEXT NOT NULL,
            sql_query TEXT NOT NULL,
            validation_score REAL NOT NULL DEFAULT 0.0,
            validated_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (ticker, statement_type)
        );
        CREATE INDEX IF NOT EXISTS idx_formulas_ticker_stmt ON financial_formulas(ticker, statement_type, validated_at DESC);
    """,
    "calculated_metrics": """
        CREATE TABLE IF NOT EXISTS calculated_metrics (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            moving_avg_3y REAL,
            std_dev_3y REAL,
            pe_ratio REAL,
            PRIMARY KEY (ticker, date)
        );
    """,
    "raw_fundamentals": """
        CREATE TABLE IF NOT EXISTS raw_fundamentals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            statement_type TEXT NOT NULL,
            period_end_date TEXT NOT NULL,
            label TEXT NOT NULL,
            concept TEXT NOT NULL,
            value REAL NOT NULL,
            unit TEXT DEFAULT 'USD',
            source TEXT DEFAULT 'SEC_EDGAR',
            extracted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ticker, statement_type, period_end_date, concept)
        );
        CREATE INDEX IF NOT EXISTS idx_raw_fundamentals_ticker_period ON raw_fundamentals(ticker, period_end_date);
        CREATE INDEX IF NOT EXISTS idx_raw_fundamentals_concept ON raw_fundamentals(concept);
        CREATE INDEX IF NOT EXISTS idx_fundamentals_ticker_date ON raw_fundamentals(ticker, statement_type, period_end_date DESC);
    """,
    "stock_prices": """
        CREATE TABLE IF NOT EXISTS stock_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            UNIQUE(ticker, date)
        );
        CREATE INDEX IF NOT EXISTS idx_prices_ticker_date ON stock_prices(ticker, date DESC);
    """
}
VEC_TABLES = {}
