# deprecated/tools/finance/metrics.py
"""PE Ratio, 3-year moving average, and standard deviation calculation pipeline.

Synchronous compute work runs in a helper that is executed via
asyncio.to_thread() so the main event loop is not blocked.
"""
import asyncio
import pandas as pd
import numpy as np
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from database.formula_cache import get_formula
from database.connection import DatabaseManager
from database.writer import enqueue_write
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

MOVING_AVG_WINDOW = 756  # ~3 trading years


def _run_formula_sql(db_path: str, ticker: str, sql: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        params = (ticker.upper(),) * sql.count('?')
        return pd.read_sql_query(sql, conn, params=params)
    except Exception as e:
        log.dual_log(
            tag="Finance:Metrics",
            message=f"Metrics formula SQL failed: {e}",
            level="WARNING",
            exc_info=e,
        )
        return pd.DataFrame()
    finally:
        conn.close()


def _compute_and_persist_metrics(db_path: str, ticker: str) -> bool:
    """Synchronous worker: runs pandas computation and enqueues DB writes.

    This function is intended to be executed inside a thread (via
    asyncio.to_thread) so it may perform blocking CPU work and DB I/O.
    """
    tag = f'METRICS_{ticker}'
    log.dual_log(
        tag=f"Finance:Metrics:{ticker}",
        message=f'Starting calculated metrics pipeline for {ticker}.',
        payload={'ticker': ticker},
        level="INFO"
    )

    earnings_sql = get_formula(ticker, 'Quarterly Earnings')
    shares_sql   = get_formula(ticker, 'Shares Outstanding')

    if not earnings_sql or not shares_sql:
        log.dual_log(
            tag="Finance:Metrics:Config",
            message=f'Cannot compute PE for {ticker}: missing Quarterly Earnings or Shares Outstanding formula.',
            level="ERROR",
            payload={'ticker': ticker},
        )
        return False

    # Fetch prices
    conn = sqlite3.connect(db_path)
    try:
        prices_df = pd.read_sql_query(
            'SELECT date, close FROM stock_prices WHERE ticker=? ORDER BY date',
            conn, params=(ticker.upper(),)
        )
    finally:
        conn.close()

    if prices_df.empty:
        log.dual_log(
            tag="Finance:Metrics:Data",
            message=f'No price data for {ticker}.',
            level="ERROR",
            payload={'ticker': ticker},
        )
        return False

    # Execute earnings + shares SQL
    earnings_df = _run_formula_sql(db_path, ticker, earnings_sql)
    shares_df   = _run_formula_sql(db_path, ticker, shares_sql)

    if earnings_df.empty or shares_df.empty:
        log.dual_log(
            tag="Finance:Metrics:Data",
            message=f'Earnings or shares formula returned empty data for {ticker}.',
            level="WARNING",
            payload={'ticker': ticker},
        )
        return False

    # Parse dates and compute TTM (trailing twelve months) earnings
    for df in [earnings_df, shares_df]:
        df['period_end_date'] = pd.to_datetime(df['period_end_date'])

    earnings_df = (
        earnings_df.drop_duplicates('period_end_date')
        .set_index('period_end_date')
        .sort_index()
    )
    shares_df = (
        shares_df.drop_duplicates('period_end_date')
        .set_index('period_end_date')
        .sort_index()
    )

    ttm_earnings = (
        earnings_df['value']
        .rolling(window=4, min_periods=4).sum()
        .to_frame('ttm_earnings')
    )

    prices_df['date'] = pd.to_datetime(prices_df['date'])
    prices_df = prices_df.set_index('date').sort_index()

    merged = pd.merge_asof(prices_df, ttm_earnings, left_index=True, right_index=True, direction='backward')
    merged = pd.merge_asof(
        merged,
        shares_df.rename(columns={'value': 'shares_outstanding'}),
        left_index=True, right_index=True, direction='backward'
    )

    # PE ratio
    merged['pe_ratio'] = (
        (merged['close'] * merged['shares_outstanding']) / merged['ttm_earnings']
    ).replace([np.inf, -np.inf], np.nan)

    # Moving average & std-dev
    merged['moving_avg_3y'] = merged['close'].rolling(MOVING_AVG_WINDOW).mean()
    merged['std_dev_3y']    = merged['close'].rolling(MOVING_AVG_WINDOW).std()

    # Persist
    result = (
        merged[['moving_avg_3y', 'std_dev_3y', 'pe_ratio']]
        .dropna(how='all')
        .reset_index()
    )
    result.columns = ['date', 'moving_avg_3y', 'std_dev_3y', 'pe_ratio']
    result['date'] = result['date'].dt.strftime('%Y-%m-%d')
    result['ticker'] = ticker.upper()

    for _, row in result.iterrows():
        enqueue_write(
            '''INSERT OR REPLACE INTO calculated_metrics
               (ticker, date, moving_avg_3y, std_dev_3y, pe_ratio)
               VALUES (?,?,?,?,?)''',
            (row['ticker'], row['date'], row.get('moving_avg_3y'),
             row.get('std_dev_3y'), row.get('pe_ratio'))
        )

    log.dual_log(
        tag=f"Finance:Metrics:{ticker}",
        message=f'Metrics pipeline complete for {ticker}. {len(result)} rows saved.',
        level="INFO",
        payload={'ticker': ticker, 'rows_saved': len(result)},
    )
    return True


async def update_calculated_metrics(
    db_path: str,
    ticker: str,
    prices_were_updated: bool = True,
    fundamentals_were_updated: bool = True,
) -> bool:
    """
    Compute and persist PE Ratio, moving avg, and std-dev for a ticker.
    Offloads CPU-bound pandas work to a background thread to avoid blocking the event loop.
    Returns True on success, False if preconditions are not met.
    """
    return await asyncio.to_thread(_compute_and_persist_metrics, db_path, ticker)
