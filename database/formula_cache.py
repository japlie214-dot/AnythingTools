# database/formula_cache.py
"""Read/write helpers for the grouped_formulas and calculated_metrics tables."""
import sqlite3
from typing import Optional
from datetime import datetime, timezone
from database.connection import DatabaseManager
from database.writer import enqueue_write


def get_formula(ticker: str, statement_type: str) -> Optional[str]:
    """Return the cached SQL script for a ticker/statement pair, or None."""
    from database.reader import execute_read_sql
    rows = execute_read_sql(
        'SELECT sql_query FROM financial_formulas WHERE ticker=? AND statement_type=?',
        (ticker.upper(), statement_type)
    )
    return rows[0]['sql_query'] if rows else None


def save_formula(ticker: str, statement_type: str, sql_script: str, score: float):
    """Persist a validated SQL formula to the cache."""
    enqueue_write(
        '''INSERT OR REPLACE INTO financial_formulas
           (ticker, statement_type, sql_query, validation_score, validated_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (ticker.upper(), statement_type, sql_script, score,
         datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat())
    )


def get_latest_fundamental_date(ticker: str, statement_type: str) -> Optional[str]:
    """Return the most recent period_end_date for a ticker/statement in the DB."""
    from database.reader import execute_read_sql
    rows = execute_read_sql(
        'SELECT MAX(period_end_date) as max_date FROM raw_fundamentals WHERE ticker=? AND statement_type=?',
        (ticker.upper(), statement_type)
    )
    return rows[0]['max_date'] if rows else None


def get_latest_price_date(ticker: str) -> Optional[str]:
    """Return the most recent date in stock_prices for a ticker."""
    from database.reader import execute_read_sql
    rows = execute_read_sql(
        'SELECT MAX(date) as max_date FROM stock_prices WHERE ticker=?',
        (ticker.upper(),)
    )
    return rows[0]['max_date'] if rows else None
