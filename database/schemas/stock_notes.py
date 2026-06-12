""" database/schemas/stock_notes.py
Database schema for stock_notes tool.
"""

TABLES = {
    "sn_filings": """CREATE TABLE IF NOT EXISTS sn_filings (
        filing_id TEXT PRIMARY KEY,
        ticker TEXT NOT NULL,
        form TEXT NOT NULL,
        filing_date TEXT NOT NULL,
        accession_no TEXT NOT NULL UNIQUE,
        period_of_report TEXT NOT NULL DEFAULT '',
        company_name TEXT NOT NULL DEFAULT '',
        cik INTEGER NOT NULL DEFAULT 0,
        fiscal_year_end_month INTEGER NOT NULL DEFAULT 12,
        quarter INTEGER NOT NULL DEFAULT 0,
        year INTEGER NOT NULL DEFAULT 0,
        content_hash TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_sn_filings_ticker ON sn_filings(ticker, form);
    CREATE INDEX IF NOT EXISTS idx_sn_filings_quarter ON sn_filings(ticker, quarter, year);""",

    "sn_notes": """CREATE TABLE IF NOT EXISTS sn_notes (
        note_id TEXT PRIMARY KEY,
        filing_id TEXT NOT NULL,
        ticker TEXT NOT NULL,
        form TEXT NOT NULL,
        accession_no TEXT NOT NULL,
        note_number INTEGER NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        short_name TEXT NOT NULL DEFAULT '',
        narrative_text TEXT NOT NULL DEFAULT '',
        narrative_hash TEXT NOT NULL DEFAULT '',
        expands TEXT NOT NULL DEFAULT '[]',
        expands_statements TEXT NOT NULL DEFAULT '[]',
        table_count INTEGER NOT NULL DEFAULT 0,
        details_count INTEGER NOT NULL DEFAULT 0,
        quarter INTEGER NOT NULL DEFAULT 0,
        year INTEGER NOT NULL DEFAULT 0,
        quarterly_status TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL DEFAULT 1,
        content_hash TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(filing_id) REFERENCES sn_filings(filing_id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_sn_notes_filing ON sn_notes(filing_id);
    CREATE INDEX IF NOT EXISTS idx_sn_notes_ticker_quarter ON sn_notes(ticker, quarter, year);""",

    "sn_detail_registry": """CREATE TABLE IF NOT EXISTS sn_detail_registry (
        registry_id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        detail_table_name TEXT NOT NULL,
        source_title TEXT NOT NULL DEFAULT '',
        source_note_number INTEGER NOT NULL DEFAULT 0,
        source_accession_no TEXT NOT NULL DEFAULT '',
        role_or_type TEXT NOT NULL DEFAULT '',
        available_concepts TEXT NOT NULL DEFAULT '[]',
        tidy_schema_version INTEGER NOT NULL DEFAULT 1,
        row_count INTEGER NOT NULL DEFAULT 0,
        quarter INTEGER NOT NULL DEFAULT 0,
        year INTEGER NOT NULL DEFAULT 0,
        quarterly_status TEXT NOT NULL DEFAULT '',
        content_hash TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(ticker, detail_table_name, source_accession_no, source_note_number)
    );
    CREATE INDEX IF NOT EXISTS idx_sn_detail_registry_ticker ON sn_detail_registry(ticker, detail_table_name);
    CREATE INDEX IF NOT EXISTS idx_sn_detail_registry_quarter ON sn_detail_registry(ticker, quarter, year);""",

    "sn_note_details": """CREATE TABLE IF NOT EXISTS sn_note_details (
        detail_id TEXT PRIMARY KEY,
        accession_no TEXT NOT NULL,
        note_number INTEGER NOT NULL,
        detail_index INTEGER NOT NULL DEFAULT 0,
        ticker TEXT NOT NULL,
        form TEXT NOT NULL,
        concept TEXT NOT NULL DEFAULT '',
        label TEXT NOT NULL DEFAULT '',
        standard_concept TEXT NOT NULL DEFAULT '',
        level INTEGER NOT NULL DEFAULT 0,
        abstract TEXT NOT NULL DEFAULT 'False',
        dimension TEXT NOT NULL DEFAULT 'False',
        is_breakdown TEXT NOT NULL DEFAULT 'False',
        dimension_axis TEXT NOT NULL DEFAULT '',
        dimension_member TEXT NOT NULL DEFAULT '',
        dimension_member_label TEXT NOT NULL DEFAULT '',
        dimension_label TEXT NOT NULL DEFAULT '',
        balance TEXT NOT NULL DEFAULT '',
        weight TEXT NOT NULL DEFAULT '',
        preferred_sign TEXT NOT NULL DEFAULT '',
        parent_concept TEXT NOT NULL DEFAULT '',
        parent_abstract_concept TEXT NOT NULL DEFAULT '',
        period_raw TEXT NOT NULL DEFAULT '',
        period_end_date TEXT NOT NULL,
        period_type TEXT NOT NULL,
        value TEXT NOT NULL DEFAULT '',
        row_order INTEGER NOT NULL DEFAULT 0,
        content_hash TEXT NOT NULL DEFAULT '',
        extracted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(accession_no) REFERENCES sn_filings(accession_no) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_sn_note_details_ticker_period ON sn_note_details(ticker, period_type, period_end_date);
    CREATE INDEX IF NOT EXISTS idx_sn_note_details_concept ON sn_note_details(concept, period_end_date);
    CREATE INDEX IF NOT EXISTS idx_sn_note_details_accession ON sn_note_details(accession_no, note_number);"""
}
