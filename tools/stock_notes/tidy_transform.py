import re
import hashlib
import pandas as pd
from typing import List, Dict, Any, Tuple

XBRL_METADATA_COLUMNS = frozenset({
    "abstract", "balance", "concept", "dimension", "dimension_axis",
    "dimension_label", "dimension_member", "dimension_member_label",
    "is_breakdown", "label", "level", "parent_abstract_concept",
    "parent_concept", "preferred_sign", "standard_concept", "weight"
})

def make_registry_id(ticker: str, detail_table_name: str, accession_no: str, note_number: int) -> str:
    """Deterministic primary key for sn_detail_registry.
    
    Same natural key always produces the same registry_id, regardless of
    INSERT OR REPLACE history. This prevents Snowflake duplicate rows when
    MERGE uses registry_id as the match key.
    """
    raw = f"{ticker}|{detail_table_name}|{accession_no}|{note_number}"
    return hashlib.md5(raw.encode("utf-8", errors="replace")).hexdigest()

def parse_period_column(col_name: str) -> dict:
    if not isinstance(col_name, str):
        col_name = str(col_name) if col_name is not None else ""
    m = re.match(r'^(\d{4}-\d{2}-\d{2})\s*\((FY|Q[1-4]|YTD|H[1-2])\)$', col_name)
    if m:
        return {"end_date": m.group(1), "period_type": m.group(2)}
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', col_name)
    type_match = re.search(r'\((FY|Q[1-4]|YTD|H[1-2])\)', col_name)
    return {
        "end_date": date_match.group(1) if date_match else "1970-01-01",
        "period_type": type_match.group(1) if type_match else "UNKNOWN",
    }

def transform_to_tidy(df: pd.DataFrame, ticker: str, form: str, accession_no: str, note_number: int, detail_idx: int) -> Tuple[List[Dict[str, Any]], List[str]]:
    df = df.copy()
    df["row_order"] = range(len(df))
    
    available_cols = list(df.columns)
    metadata_cols = [c for c in available_cols if c in XBRL_METADATA_COLUMNS or c == "row_order"]
    period_cols = [c for c in available_cols if c not in XBRL_METADATA_COLUMNS and c != "row_order"]
    
    if not period_cols:
        return [], []
        
    tidy = df.melt(id_vars=metadata_cols, value_vars=period_cols, var_name="period_raw", value_name="value")
    
    parsed = tidy["period_raw"].apply(parse_period_column)
    tidy["period_end_date"] = parsed.apply(lambda p: p["end_date"])
    tidy["period_type"] = parsed.apply(lambda p: p["period_type"])
    
    tidy["value"] = tidy["value"].apply(lambda v: "" if pd.isna(v) else str(v))
    for bool_col in ["abstract", "dimension", "is_breakdown"]:
        if bool_col in tidy.columns:
            tidy[bool_col] = tidy[bool_col].apply(str)
            
    records = tidy.to_dict(orient="records")
    
    for r in records:
        r["accession_no"] = accession_no
        r["note_number"] = int(note_number) if note_number is not None else 0
        r["detail_index"] = int(detail_idx) if detail_idx is not None else 0
        r["ticker"] = ticker
        r["form"] = form
        
        concept_val = str(r.get("concept")) if r.get("concept") is not None and not pd.isna(r.get("concept")) else ""
        period_raw_val = str(r.get("period_raw")) if r.get("period_raw") is not None and not pd.isna(r.get("period_raw")) else ""
        row_order_val = int(r.get("row_order")) if r.get("row_order") is not None and not pd.isna(r.get("row_order")) else 0
        
        detail_id_parts = [accession_no, str(r["note_number"]), str(r["detail_index"]), concept_val, period_raw_val, str(row_order_val)]
        r["detail_id"] = "|".join(detail_id_parts)
        
        hash_parts = [r["detail_id"], r.get("value", "")]
        r["content_hash"] = hashlib.md5("||".join(hash_parts).encode("utf-8", errors="replace")).hexdigest()
        
        for meta_col in XBRL_METADATA_COLUMNS:
            val = r.get(meta_col)
            if val is None or pd.isna(val):
                r[meta_col] = "" if meta_col != "level" else 0
            else:
                if meta_col == "level":
                    try:
                        r[meta_col] = int(float(val))
                    except Exception:
                        r[meta_col] = 0
                else:
                    r[meta_col] = str(val)
                    
    unique_concepts = [str(c) for c in tidy["concept"].dropna().unique() if c] if "concept" in tidy.columns else []
    return records, unique_concepts
