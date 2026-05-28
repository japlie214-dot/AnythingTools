""" tools/stock_notes/fiscal.py
Fiscal quarter utilities for stock_notes.
All fiscal year-end detection is dynamic (no hardcoding).
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime
from typing import Optional, Tuple, List

from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

# Cache: ticker -> fiscal_year_end_month to avoid repeated API calls
_fy_month_cache: dict[str, int] = {}

def get_fiscal_year_end_month(ticker: str, company=None) -> int:
    """
    Dynamically detect the fiscal year-end month for any ticker.
    Three-tier detection strategy:
      1. company.fiscal_year_end — SEC Submissions API field ("MMDD" format)
      2. XBRL entity_info — from filing financials
      3. Heuristic — most common month from annual period_of_report dates
    """
    ticker_upper = ticker.upper()
    if ticker_upper in _fy_month_cache:
        return _fy_month_cache[ticker_upper]
    
    fy_month = _detect_fy_month(ticker_upper, company)
    _fy_month_cache[ticker_upper] = fy_month
    return fy_month

def _detect_fy_month(ticker: str, company=None) -> int:
    from edgar import Company
    
    # Tier 1
    try:
        if company is None:
            company = Company(ticker)
        fye = company.fiscal_year_end
        if fye and isinstance(fye, str) and len(fye) >= 2:
            month = int(fye[:2])
            if 1 <= month <= 12:
                return month
    except Exception:
        pass

    # Tier 2
    try:
        if company is None:
            company = Company(ticker)
        filings = company.get_filings(form="10-K", amendments=False)
        if filings and len(filings) > 0:
            obj = filings[0].obj()
            if hasattr(obj, "financials") and obj.financials is not None:
                fin = obj.financials
                if hasattr(fin, "xb") and fin.xb is not None:
                    fy_month_xbrl = fin.xb.entity_info.get("fiscal_year_end_month")
                    if fy_month_xbrl and isinstance(fy_month_xbrl, int):
                        if 1 <= fy_month_xbrl <= 12:
                            return fy_month_xbrl
    except Exception:
        pass

    # Tier 3
    try:
        if company is None:
            company = Company(ticker)
        filings = company.get_filings(form="10-K", amendments=False)
        if filings:
            months = []
            for f in list(filings)[:5]:
                obj = f.obj()
                period = getattr(obj, "period_of_report", None)
                if period:
                    try:
                        pd_date = datetime.strptime(str(period), "%Y-%m-%d").date()
                        months.append(pd_date.month)
                    except ValueError:
                        pass
            if months:
                most_common = Counter(months).most_common(1)[0][0]
                if 1 <= most_common <= 12:
                    return most_common
    except Exception:
        pass

    log.dual_log(tag="StockNotes:Fiscal:Fallback", message=f"All FYE detection tiers failed for {ticker}, defaulting to 12", level="WARNING", payload={"ticker": ticker})
    return 12


def fiscal_quarter_from_period_end(period_end: date, fiscal_year_end_month: int) -> Tuple[int, int]:
    m = period_end.month
    q = ((m - fiscal_year_end_month - 1) % 12) // 3 + 1
    fy = period_end.year if m <= fiscal_year_end_month else period_end.year + 1
    if m == fiscal_year_end_month:
        q = 4
        fy = period_end.year
    return (q, fy)


def parse_quarter_date(date_str: str) -> Tuple[int, int]:
    try:
        parts = date_str.strip().split("-")
        year = int(parts[0])
        month = int(parts[1])
        if 1 <= month <= 12 and year > 1900:
            return (year, month)
    except (ValueError, IndexError):
        pass
    return (0, 0)


def quarter_date_range(start_date: str, end_date: str, fiscal_year_end_month: int = 12, max_quarters: int = 12) -> List[Tuple[int, int]]:
    start_y, start_m = parse_quarter_date(start_date) if start_date else (0, 0)
    end_y, end_m = parse_quarter_date(end_date) if end_date else (0, 0)
    
    if start_y == 0 or end_y == 0:
        return []
    
    start_dt = date(start_y, start_m, 1)
    end_dt = date(end_y, end_m, 1)
    start_q, start_fy = fiscal_quarter_from_period_end(start_dt, fiscal_year_end_month)
    end_q, end_fy = fiscal_quarter_from_period_end(end_dt, fiscal_year_end_month)
    
    quarters = []
    current_q, current_fy = start_q, start_fy
    while (current_fy, current_q) <= (end_fy, end_q):
        quarters.append((current_q, current_fy))
        current_q += 1
        if current_q > 4:
            current_q = 1
            current_fy += 1
        if len(quarters) >= max_quarters:
            break
    
    return quarters
