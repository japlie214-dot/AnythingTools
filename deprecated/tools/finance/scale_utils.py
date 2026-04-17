# deprecated/tools/finance/scale_utils.py
"""
Detects and corrects scale mismatches between yfinance (thousands) and
EDGAR (absolute) data sources before SQL validation.
"""
import pandas as pd
import numpy as np
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

# Anchor map: human-readable name -> EDGAR GAAP concept string
_ANCHOR_MAP = {
    'Total Revenue':            'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax',
    'Operating Income':         'us-gaap:OperatingIncomeLoss',
    'Gross Profit':             'us-gaap:GrossProfit',
    'Total Assets':             'us-gaap:Assets',
    'Total Liabilities':        'us-gaap:Liabilities',
    'Total Stockholders Equity':'us-gaap:StockholdersEquity',
}


def detect_and_apply_scale(
    reference_df: pd.DataFrame,
    raw_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Compare anchor metrics between reference (yfinance) and raw (EDGAR) data.
    If yfinance reports in thousands or millions, scale down the reference DF
    so numeric validation can succeed.
    Returns the (potentially scaled) reference_df.
    """
    if reference_df.empty or raw_df.empty:
        return reference_df

    # Build indexed lookup: (concept, period_end_date) -> value
    if 'concept' not in raw_df.columns or 'period_end_date' not in raw_df.columns:
        return reference_df

    raw_indexed = raw_df.set_index(['concept', 'period_end_date'])
    latest_date = sorted(reference_df.columns, reverse=True)[0] if not reference_df.columns.empty else None
    if not latest_date:
        return reference_df

    for ref_name, raw_concept in _ANCHOR_MAP.items():
        if ref_name not in reference_df.index:
            continue
        if (raw_concept, latest_date) not in raw_indexed.index:
            continue
        try:
            ref_val = abs(float(reference_df.loc[ref_name, latest_date]))
            raw_val = abs(float(raw_indexed.loc[(raw_concept, latest_date), 'value']))
            
            # Defensive check against invalid math
            if pd.isna(ref_val) or pd.isna(raw_val) or raw_val < 1:
                continue
                
            ratio = ref_val / raw_val
            
            # Apply dynamic scaling
            if 950 < ratio < 1050:
                log.dual_log(tag="Finance:Scale", message=f"Scaling {ref_name} by 1,000x", level="INFO", payload={"ref_name": ref_name})
                return reference_df / 1000.0
            elif 950000 < ratio < 1050000:
                log.dual_log(tag="Finance:Scale", message=f"Scaling {ref_name} by 1,000,000x", level="INFO", payload={"ref_name": ref_name})
                return reference_df / 1000000.0
                
        except Exception as e:
            log.dual_log(tag="Finance:Scale", message=f"Scale correction skipped for {ref_name}: {e}", level="DEBUG", payload={"ref_name": ref_name, "error": repr(e)})
            continue

    return reference_df
