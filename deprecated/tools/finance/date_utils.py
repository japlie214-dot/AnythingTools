# deprecated/tools/finance/date_utils.py
import pandas as pd
from typing import List

def align_yfinance_to_edgar_dates(
    yf_dates: List[str],
    edgar_dates: List[str],
) -> List[str]:
    '''
    Fuzzy-match YFinance period-end dates to EDGAR filing dates.
    Matches on YYYY-MM with +/- 1 month tolerance.
    Returns a list of EDGAR dates in the same order as yf_dates.
    '''
    edgar_month_map = {}
    for d in sorted(edgar_dates, reverse=True):
        month_key = d[:7]   # YYYY-MM
        edgar_month_map.setdefault(month_key, d)

    aligned: List[str] = []
    used: set = set()

    for yf_d in yf_dates:
        ts = pd.Timestamp(yf_d)
        candidates = [
            ts.strftime('%Y-%m'),                          # exact month
            (ts + pd.DateOffset(months=1)).strftime('%Y-%m'), # +1
            (ts - pd.DateOffset(months=1)).strftime('%Y-%m'), # -1
        ]
        matched = None
        for key in candidates:
            if key in edgar_month_map and edgar_month_map[key] not in used:
                matched = edgar_month_map[key]
                used.add(matched)
                break
        if matched:
            aligned.append(matched)

    return aligned
