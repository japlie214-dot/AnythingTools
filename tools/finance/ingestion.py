# tools/finance/ingestion.py
"""
SEC EDGAR Financial Data Ingestion Module

Provides automated ingestion of raw financial data from SEC EDGAR filings
(10-Q, 10-K) and normalizes it into long-format database storage.
"""

import asyncio
import pandas as pd
from typing import Optional, Dict, Any, List
from datetime import datetime
import re

from database.writer import enqueue_write
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

# Try to import edgar tools, provide fallback if not available
try:
    import edgar
    from edgar import Company, Financials
    EDGAR_AVAILABLE = True
except ImportError:
    EDGAR_AVAILABLE = False
    log.dual_log(
        tag="Finance:Ingestion",
        message="edgartools not available - SEC ingestion will be limited to mocked data for testing",
        level="WARNING",
    )


def _clean_value(value: Any) -> float:
    """
    Clean and convert financial values from string to float.
    
    Handles:
    - Currency symbols ($, €, etc.)
    - Parentheses for negative values
    - Comma separators
    - Thousands/millions abbreviations
    """
    if value is None:
        return 0.0
    
    if isinstance(value, (int, float)):
        return float(value)
    
    if not isinstance(value, str):
        return 0.0
    
    # Remove whitespace
    cleaned = value.strip()
    
    # Handle parenthetical negatives
    is_negative = False
    if cleaned.startswith('(') and cleaned.endswith(')'):
        is_negative = True
        cleaned = cleaned[1:-1]
    
    # Remove currency symbols and commas
    cleaned = cleaned.replace('$', '').replace('€', '').replace(',', '').replace(' ', '')
    
    # Handle thousands/millions/billions abbreviations (like "1.2B" or "1,234M")
    multiplier = 1.0
    if cleaned.lower().endswith('b'):
        multiplier = 1_000_000_000
        cleaned = cleaned[:-1]
    elif cleaned.lower().endswith('m'):
        multiplier = 1_000_000
        cleaned = cleaned[:-1]
    elif cleaned.lower().endswith('k'):
        multiplier = 1_000
        cleaned = cleaned[:-1]
    
    try:
        result = float(cleaned) * multiplier
        return -result if is_negative else result
    except (ValueError, TypeError):
        return 0.0


def _normalize_period_date(date_str: str) -> str:
    """
    Normalize various date formats to YYYY-MM-DD.
    """
    if not date_str:
        return datetime.now().strftime('%Y-%m-%d')
    
    # Handle common formats
    formats = [
        '%Y-%m-%d',
        '%m/%d/%Y',
        '%d/%m/%Y',
        '%b %d, %Y',
        '%B %d, %Y',
        '%Y-%m-%dT%H:%M:%S',
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            continue
    
    # If all parsing fails, return current date as fallback
    log.dual_log(
        tag="Finance:Ingestion",
        message=f"Could not parse date: {date_str}, using current date",
        level="WARNING",
    )
    return datetime.now().strftime('%Y-%m-%d')


def _get_or_create_cached_filing(ticker: str, form_type: str = "10-Q", quarters: int = 4) -> Optional[Any]:
    """
    Get cached filing data or fetch new if needed.
    
    This is a simplified version that would typically cache filings to avoid
    repeated API calls. For production use, implement proper caching.
    """
    if not EDGAR_AVAILABLE:
        return None
    
    try:
        # Set identity for EDGAR access
        edgar.set_identity("SumAnal Agent bot@example.com")
        company = Company(ticker)
        
        # Get recent filings
        filings = company.get_filings(form=form_type).head(quarters)
        
        return filings if not filings.empty else None
        
    except Exception as e:
        log.dual_log(
            tag="Finance:Ingestion",
            message=f"Failed to fetch EDGAR filings for {ticker}: {e}",
            level="ERROR",
            exc_info=e,
        )
        return None


def _stepped_extract(filings, num_quarters: int):
    '''
    Attempt EDGAR MultiFinancials extraction with progressive fallback.
    Tries full amount, then 30, 20, 12 quarters until success.
    '''
    steps = sorted(set([num_quarters, 30, 20, 12]), reverse=True)
    steps = [n for n in steps if n <= num_quarters]
    if not steps:
        steps = [num_quarters]

    for n in steps:
        try:
            log.dual_log(
                tag="Finance:Ingestion",
                message=f'Extracting financials: attempting {n} quarters...',
                level="INFO",
            )
            subset = filings.head(n)
            result = edgar.MultiFinancials.extract(filings=subset)
            log.dual_log(
                tag="Finance:Ingestion",
                message=f'Extraction succeeded with {n} quarters.',
                level="INFO",
            )
            return result
        except Exception as e:
            log.dual_log(
                tag="Finance:Ingestion",
                message=f'Extraction failed at {n} quarters: {e}',
                level="WARNING",
                exc_info=e,
            )
            if n == steps[-1]:
                log.dual_log(
                    tag="Finance:Ingestion",
                    message='All extraction attempts exhausted.',
                    level="ERROR",
                )
                return None
            log.dual_log(
                tag="Finance:Ingestion",
                message='Retrying with smaller batch...',
                level="INFO",
            )
    return None


async def ingest_sec_fundamentals(ticker: str, statement_type: str = "All", num_quarters: int = 4) -> Dict[str, Any]:
    """
    Fetches 10-Q/10-K filings, normalizes to long-format, and saves to DB.
    
    Args:
        ticker: Stock ticker symbol
        statement_type: 'Income Statement', 'Balance Sheet', 'Cash Flow', or 'All'
        num_quarters: Number of quarters to fetch (default: 4)
        
    Returns:
        Dict with status and statistics
    """
    if not EDGAR_AVAILABLE:
        # Fallback mock data for testing without edgartools
        log.dual_log(
            tag="Finance:Ingestion",
            message="edgartools not available - using mock data",
            level="WARNING",
        )
        return await _ingest_mock_fundamentals(ticker, statement_type, num_quarters)
    
    # Fetch filings
    filings = _get_or_create_cached_filing(ticker, "10-Q", num_quarters)
        
    if not filings:
        return {
            "status": "error",
            "message": f"No filings found for {ticker}",
            "records_inserted": 0
        }
    
    # Extract financials using stepped extraction
    financials = _stepped_extract(filings, num_quarters)
    
    if financials is None:
        # Fallback to single filing approach
        try:
            company = Company(ticker)
            filing_list = company.get_filings(form="10-Q")
            if len(filing_list) == 0:
                raise ValueError("No filings available.")
            # Safely get the most recent filing whether it's a DataFrame or edgartools Filings object
            filing = filing_list.iloc[0] if hasattr(filing_list, "iloc") else filing_list[0]
            financials = edgar.Financials.extract(filing)
        except Exception as extraction_error:
            return {
                "status": "error",
                "message": f"Failed to extract financials: {extraction_error}",
                "records_inserted": 0
            }
    
    statements = []
        
    if statement_type == "All" or statement_type == "Income Statement":
        try:
            income_stmt = financials.income_statement()
            if income_stmt is not None:
                statements.append(('Income Statement', income_stmt))
        except Exception as e:
            log.dual_log(
                tag="Finance:Ingestion",
                message=f"Could not extract income statement: {e}",
                level="WARNING",
                exc_info=e,
            )
    
    if statement_type == "All" or statement_type == "Balance Sheet":
        try:
            balance_sheet = financials.balance_sheet()
            if balance_sheet is not None:
                statements.append(('Balance Sheet', balance_sheet))
        except Exception as e:
            log.dual_log(
                tag="Finance:Ingestion",
                message=f"Could not extract balance sheet: {e}",
                level="WARNING",
                exc_info=e,
            )
    
    if statement_type == "All" or statement_type == "Cash Flow":
        try:
            cash_flow = financials.cashflow_statement()
            if cash_flow is not None:
                statements.append(('Cash Flow', cash_flow))
        except Exception as e:
            log.dual_log(
                tag="Finance:Ingestion",
                message=f"Could not extract cash flow: {e}",
                level="WARNING",
                exc_info=e,
            )
    
    if not statements:
        return {
            "status": "error",
            "message": f"No statements extracted for {ticker}",
            "records_inserted": 0
        }
    
    # Process and normalize each statement
    total_inserted = 0
    
    for name, stmt in statements:
        try:
            df = stmt.to_dataframe()
            
            # Unpivot/Melt into normalized format: ticker, statement, date, label, concept, value
            # Handle both wide (dates as columns) and long (dates in rows) formats
            if 'label' in df.columns and 'concept' in df.columns:
                # Identify value columns (dates)
                value_cols = [col for col in df.columns if col not in ['label', 'concept']]
                
                if value_cols:
                    # Wide format - melt it
                    melted = df.melt(
                        id_vars=['label', 'concept'], 
                        var_name='period_end_date', 
                        value_name='value'
                    )
                else:
                    # Already long format or no value columns
                    if 'value' in df.columns:
                        melted = df
                    else:
                        log.dual_log(
                            tag="Finance:Ingestion",
                            message=f"Could not determine format for {name}",
                            level="WARNING",
                        )
                        continue
            else:
                # Fallback parsing - assume index contains labels
                if 'label' not in df.columns:
                    if hasattr(df, 'index'):
                        df = df.reset_index()
                        if 'index' in df.columns:
                            df = df.rename(columns={'index': 'label'})
                
                # Try to identify concept column
                if 'concept' not in df.columns:
                    # Use label as concept if no separate concept column
                    df['concept'] = df['label']
                
                value_cols = [col for col in df.columns if col not in ['label', 'concept']]
                if value_cols:
                    melted = df.melt(
                        id_vars=['label', 'concept'],
                        var_name='period_end_date',
                        value_name='value'
                    )
                else:
                    continue
            
            # Clean and normalize data
            for _, row in melted.iterrows():
                try:
                    label = str(row['label']) if pd.notna(row['label']) else "Unknown"
                    concept = str(row['concept']) if pd.notna(row['concept']) else label
                    period_date = _normalize_period_date(str(row['period_end_date']))
                    cleaned_value = _clean_value(row['value'])
                    
                    # Skip zeros or invalid values
                    if abs(cleaned_value) < 0.01:
                        continue
                    
                    # Enqueue write
                    enqueue_write(
                        """
                        INSERT OR REPLACE INTO raw_fundamentals 
                        (ticker, statement_type, period_end_date, label, concept, value, unit, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (ticker, name, period_date, label, concept, cleaned_value, 'USD', 'SEC_EDGAR')
                    )
                    total_inserted += 1
                    
                except Exception as row_error:
                    log.dual_log(
                        tag="Finance:Ingestion",
                        message=f"Skipping row due to error: {row_error}",
                        level="DEBUG",
                    )
                    continue
                    
        except Exception as stmt_error:
            log.dual_log(
                tag="Finance:Ingestion",
                message=f"Error processing {name}: {stmt_error}",
                level="WARNING",
            )
            continue
        
    result = {
        "status": "success",
        "message": f"Successfully ingested fundamentals for {ticker}",
        "records_inserted": total_inserted,
        "statements_processed": [s[0] for s in statements]
    }
    
    log.dual_log(
        tag="Finance:Ingestion",
        message=f"SEC ingestion complete: {total_inserted} records for {ticker}",
        level="INFO",
        payload=result,   # full dict: status, message, records_inserted, statements_processed
    )
    
    return result


async def _ingest_mock_fundamentals(ticker: str, statement_type: str, num_quarters: int) -> Dict[str, Any]:
    """
    Mock ingestion for testing without edgartools installed.
    
    Creates realistic-looking financial data for demonstration purposes.
    """
    import random
    from datetime import datetime, timedelta
    
    base_date = datetime.now()
    total_inserted = 0
    
    # Define mock financial concepts for each statement type
    mock_concepts = {
        "Income Statement": [
            ("Total Revenue", "Revenue"),
            ("Net Income", "NetIncome"),
            ("Gross Profit", "GrossProfit"),
            ("Operating Expenses", "OperatingExpenses"),
            ("EBITDA", "EBITDA"),
        ],
        "Balance Sheet": [
            ("Total Assets", "Assets"),
            ("Total Liabilities", "Liabilities"),
            ("Shareholders Equity", "Equity"),
            ("Cash and Cash Equivalents", "Cash"),
            ("Current Assets", "CurrentAssets"),
        ],
        "Cash Flow": [
            ("Operating Cash Flow", "OperatingCashFlow"),
            ("Investing Cash Flow", "InvestingCashFlow"),
            ("Financing Cash Flow", "FinancingCashFlow"),
            ("Free Cash Flow", "FreeCashFlow"),
        ]
    }
    
    statements_to_generate = []
    
    if statement_type == "All":
        statements_to_generate = list(mock_concepts.keys())
    elif statement_type in mock_concepts:
        statements_to_generate = [statement_type]
    else:
        return {"status": "error", "message": "Invalid statement type", "records_inserted": 0}
    
    for stmt_type in statements_to_generate:
        concepts = mock_concepts[stmt_type]
        
        for quarter_offset in range(num_quarters):
            # Generate period end date (last day of quarter)
            period_date = base_date - timedelta(days=90 * quarter_offset)
            # Adjust to quarter end (Mar 31, Jun 30, Sep 30, Dec 31)
            month = period_date.month
            if month <= 3:
                period_date = period_date.replace(month=3, day=31)
            elif month <= 6:
                period_date = period_date.replace(month=6, day=30)
            elif month <= 9:
                period_date = period_date.replace(month=9, day=30)
            else:
                period_date = period_date.replace(month=12, day=31)
            
            period_str = period_date.strftime('%Y-%m-%d')
            
            for label, concept in concepts:
                # Generate realistic-looking values based on statement type
                if "Revenue" in label or "Income" in label:
                    value = random.uniform(1000000000, 5000000000)  # 1-5B
                elif "Cash" in label:
                    value = random.uniform(100000000, 1000000000)   # 100M-1B
                elif "Assets" in label or "Liabilities" in label:
                    value = random.uniform(5000000000, 20000000000)  # 5-20B
                else:
                    value = random.uniform(100000000, 5000000000)
                
                # Add some trend logic for consistency
                trend_factor = 1.0 + (0.05 * (num_quarters - 1 - quarter_offset))
                final_value = value * trend_factor
                
                enqueue_write(
                    """
                    INSERT OR REPLACE INTO raw_fundamentals
                    (ticker, statement_type, period_end_date, label, concept, value, unit, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ticker, stmt_type, period_str, label, concept, final_value, 'USD', 'MOCK')
                )
                total_inserted += 1
    
    result = {
        "status": "success (mock)",
        "message": f"Generated mock fundamentals for {ticker}",
        "records_inserted": total_inserted,
        "statements_processed": statements_to_generate
    }
    
    log.dual_log(
        tag="Finance:Ingestion",
        message=f"Mock ingestion complete: {total_inserted} records for {ticker}",
        level="INFO",
        payload=result,   # full dict: status, message, records_inserted, statements_processed
    )
    
    return result


async def query_fundamentals(ticker: str, concept: str, period_start: str, period_end: str) -> List[Dict[str, Any]]:
    """
    Query ingested fundamentals from database.
    
    Args:
        ticker: Stock ticker
        concept: Financial concept to query
        period_start: Start date (YYYY-MM-DD)
        period_end: End date (YYYY-MM-DD)
        
    Returns:
        List of matching records
    """
    from database.connection import DatabaseManager
    import sqlite3
    
    try:
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        
        cur = conn.execute(
            """
            SELECT ticker, statement_type, period_end_date, label, concept, value, unit, source, extracted_at
            FROM raw_fundamentals
            WHERE ticker = ? AND concept = ? AND period_end_date BETWEEN ? AND ?
            ORDER BY period_end_date DESC
            """,
            (ticker, concept, period_start, period_end)
        )
        
        rows = cur.fetchall()
        return [dict(row) for row in rows]
        
    except Exception as e:
        log.dual_log(
            tag="Finance:Ingestion",
            message=f"Query failed: {e}",
            level="ERROR",
            exc_info=e,
        )
        return []
