# tools/finance/pipeline.py
import asyncio
from datetime import datetime, timedelta
import pandas as pd
from database.connection import DatabaseManager
from tools.finance.ingestion import ingest_sec_fundamentals
from tools.finance.reconciler import FinancialReconciler
from utils.logger import get_dual_logger
import yfinance

log = get_dual_logger(__name__)

STATEMENT_TYPES = ['Income Statement', 'Balance Sheet', 'Cash Flow']

def _latest_fundamental_date(ticker: str) -> str | None:
    from database.reader import execute_read_sql
    rows = execute_read_sql(
        'SELECT MAX(period_end_date) as max_date FROM raw_fundamentals WHERE ticker=?',
        (ticker,)
    )
    return rows[0]['max_date'] if rows else None

def _is_data_stale(ticker: str, stale_days: int = 45) -> bool:
    latest = _latest_fundamental_date(ticker)
    if not latest:
        return True  # No data at all
    cutoff = (datetime.now() - timedelta(days=stale_days)).strftime('%Y-%m-%d')
    return latest < cutoff

async def _update_stock_prices(ticker: str) -> None:
    """Helper function to update stock prices for a ticker."""
    # This will be implemented in Section 3.11
    pass

async def _fetch_yfinance_ref(ticker: str, statement_type: str) -> dict:
    """
    Returns a dict keyed by ISO date string (YYYY-MM-DD),
    mapping to the aggregate value for the primary metric of that statement type.
    Shape: {"2024-09-28": 94930000000.0, "2024-06-29": 85777000000.0, ...}
    """
    try:
        yf_ticker = yfinance.Ticker(ticker)
        
        if statement_type == 'Income Statement':
            data = yf_ticker.quarterly_financials
            primary_metric = "Total Revenue"
        elif statement_type == 'Balance Sheet':
            data = yf_ticker.quarterly_balance_sheet
            primary_metric = "Total Assets"
        elif statement_type == 'Cash Flow':
            data = yf_ticker.quarterly_cashflow
            primary_metric = "Operating Cash Flow"
        else:
            return {}
        
        if data is None or data.empty or primary_metric not in data.index:
            return {}
        
        row = data.loc[primary_metric]
        result = {}
        for col in row.index:
            val = row[col]
            if pd.notna(val):
                date_str = pd.Timestamp(col).strftime('%Y-%m-%d')
                result[date_str] = float(val)
        return result
        
    except Exception as e:
        log.dual_log(tag="Finance:Pipeline", message=f"Failed to fetch YFinance ref for {ticker} {statement_type}: {e}", level="WARNING", payload={"ticker": ticker, "statement_type": statement_type, "error": repr(e)})
        return {}

async def run_financial_pipeline(
    ticker: str,
    force_refresh: bool = False,
    num_quarters: int = 12,
) -> dict:
    """
    Orchestrates the complete financial pipeline with freshness checks.
    
    Args:
        ticker: Stock ticker symbol
        force_refresh: Skip freshness check and force data refresh
        num_quarters: Number of quarters to process
        
    Returns:
        Dictionary containing reconciliation results for each statement type
    """
    ticker = ticker.upper()
    stale = _is_data_stale(ticker) or force_refresh

    if stale:
        log.dual_log(tag="Finance:Pipeline", message=f'Data stale for {ticker}. Running ingestion...', level="INFO", payload={"ticker": ticker})
        await ingest_sec_fundamentals(ticker, 'All', num_quarters)
        
        # Section 3.11 will provide this implementation
        # await _update_stock_prices(ticker)

    results = {}
    async def _reconcile(st):
        ref = await _fetch_yfinance_ref(ticker, st)
        if not ref:
            return
        r = FinancialReconciler(ticker, st)
        r.set_reference_values(ref)
        sql, score = await r.generate_validated_sql()
        results[st] = {'sql': sql, 'score': score}

    await asyncio.gather(*[_reconcile(st) for st in STATEMENT_TYPES])
    return results
