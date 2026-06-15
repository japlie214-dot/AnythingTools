# tools/stock_financials/extractor.py
import re
import json
import hashlib
from datetime import datetime, timezone
import pandas as pd
from typing import Dict, List, Tuple, Any

from utils.logger import get_dual_logger
from utils.edgar_rate_limiter import edgar_limiter
from database.writer import enqueue_transaction
from database.backup.writer.cloud_writer import enqueue_cloud_write_batch, enqueue_cloud_delete

log = get_dual_logger(__name__)

MAX_QUARTERLY_DURATION_DAYS = 95
PER_SHARE_UNITS = {"USD per share", "USD/shares"}
SHARE_UNITS = {"shares"}

def _parse_quarter_label(qlabel: str) -> Tuple[int, int]:
    try:
        parts = qlabel.split("-Q")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 0, 0

def _get_quarter_date_map(df: pd.DataFrame) -> Dict[str, pd.Timestamp]:
    date_map = {}
    if df.empty or "quarter_label" not in df.columns or "period_end" not in df.columns:
        return date_map
    for ql in df["quarter_label"].unique():
        subset = df[df["quarter_label"] == ql]
        dates = pd.to_datetime(subset["period_end"], errors="coerce").dropna()
        if len(dates) > 0:
            date_map[ql] = dates.median()
    return date_map

def _sort_quarters_desc(quarter_labels: List[str], date_map: Dict[str, pd.Timestamp] = None) -> List[str]:
    if date_map:
        return sorted(quarter_labels, key=lambda q: date_map.get(q, pd.Timestamp.min), reverse=True)
    return sorted(quarter_labels, key=_parse_quarter_label, reverse=True)

def _period_end_to_quarter_label(period_end, fiscal_period: str) -> str:
    if not period_end: return "Unknown"
    try:
        dt = pd.Timestamp(period_end)
        year = dt.year
        if fiscal_period == "Q4" and dt.month <= 3:
            year -= 1
        return f"{year}-{fiscal_period}"
    except Exception:
        return f"{period_end}-{fiscal_period}"

def compute_fact_hash(record: dict) -> str:
    parts = [record.get("ticker", ""), record.get("statement_type", ""), record.get("concept", ""), record.get("quarter", ""), str(record.get("numeric_value", ""))]
    return hashlib.md5("||".join(parts).encode("utf-8", errors="replace")).hexdigest()

def normalize_concept(concept: str) -> str:
    return concept.replace(":", "_") if concept else concept

def _is_non_additive_concept(concept: str) -> bool:
    patterns = [r"EarningsPerShare", r"WeightedAverageNumberOf.*Shares", r"CommonStockDividendsPerShare", r"AntidilutiveSecurities"]
    for p in patterns:
        if re.search(p, concept): return True
    return False

def get_statement_blueprint(facts, statement_type: str) -> pd.DataFrame:
    methods = {"income": "income_statement", "balance": "balance_sheet", "cashflow": "cashflow_statement"}
    try:
        stmt_obj = getattr(facts, methods[statement_type])()
        if stmt_obj is None: return pd.DataFrame()
        df = stmt_obj.to_dataframe()
        if df.empty: return df
        rows = []
        for idx, r in df.iterrows():
            concept_short = str(idx)
            concept_full = f"us-gaap:{concept_short}"
            rows.append({
                "concept": concept_full,
                "label": str(r.get("label", concept_short)),
                "depth": int(r.get("depth", 0)),
                "is_abstract": bool(r.get("is_abstract", False)),
                "is_total": bool(r.get("is_total", False)),
            })
        return pd.DataFrame(rows)
    except Exception as e:
        log.dual_log(tag="StockFin:Blueprint:Error", message=f"Failed getting blueprint: {e}", level="WARNING", payload={"error": str(e)})
        return pd.DataFrame()

def _enrich_with_blueprint(df: pd.DataFrame, blueprint: pd.DataFrame) -> pd.DataFrame:
    label_map = dict(zip(blueprint["concept"], blueprint["label"]))
    depth_map = dict(zip(blueprint["concept"], blueprint["depth"]))
    total_map = dict(zip(blueprint["concept"], blueprint["is_total"]))
    order_map = {c: i for i, c in enumerate(blueprint["concept"])}
    
    df["label"] = df["concept"].map(label_map).fillna(df["concept"].apply(lambda c: c.split(":")[-1]))
    df["depth"] = df["concept"].map(depth_map).fillna(0).astype(int)
    df["is_total"] = df["concept"].map(total_map).fillna(False).astype(bool)
    df["concept_order"] = df["concept"].map(order_map).fillna(999).astype(int)
    return df

def _limit_quarters(df: pd.DataFrame, num_quarters: int) -> pd.DataFrame:
    if df.empty or num_quarters <= 0: return df
    all_q = _sort_quarters_desc(df["quarter_label"].unique().tolist(), date_map=_get_quarter_date_map(df))
    return df[df["quarter_label"].isin(all_q[:num_quarters])].reset_index(drop=True)

def _derive_q4_from_fy(all_facts_df: pd.DataFrame, concepts: List[str], existing_df: pd.DataFrame, statement_type: str) -> pd.DataFrame:
    additive = [c for c in concepts if not _is_non_additive_concept(c)]
    if not additive: return existing_df
    
    existing_q4 = existing_df[existing_df["fiscal_period"] == "Q4"][["concept", "period_start"]].drop_duplicates() if "period_start" in existing_df.columns else pd.DataFrame()
    fy_data = all_facts_df[(all_facts_df["concept"].isin(additive)) & (all_facts_df["fiscal_period"] == "FY") & (all_facts_df["period_type"] == "duration")].copy()
    ytd_q3_data = all_facts_df[(all_facts_df["concept"].isin(additive)) & (all_facts_df["fiscal_period"] == "Q3") & (all_facts_df["period_type"] == "duration")].copy()
    if fy_data.empty: return existing_df

    for d in [fy_data, ytd_q3_data]:
        if d.empty: continue
        d["period_start_dt"] = pd.to_datetime(d["period_start"], errors="coerce")
        d["period_end_dt"] = pd.to_datetime(d["period_end"], errors="coerce")
        d["calc_duration"] = (d["period_end_dt"] - d["period_start_dt"]).dt.days
        
    ytd_q3_data = ytd_q3_data.sort_values(["concept", "period_start", "calc_duration"], ascending=[True, True, False]).drop_duplicates(["concept", "period_start"], keep="first") if not ytd_q3_data.empty else ytd_q3_data
    fy_data = fy_data.sort_values(["concept", "period_start", "calc_duration"], ascending=[True, True, False]).drop_duplicates(["concept", "period_start"], keep="first")
    
    derived = []
    for _, fy in fy_data.iterrows():
        c, ps, fv, pe, fy_yr, unit = fy["concept"], fy["period_start"], fy["numeric_value"], fy["period_end"], fy["fiscal_year"], fy.get("unit", "USD")
        if pd.isna(fv): continue
        if len(existing_df[(existing_df["concept"] == c) & (existing_df["fiscal_period"] == "Q4") & (existing_df["period_end"] == pe)]) > 0: continue
        
        mq3 = ytd_q3_data[(ytd_q3_data["concept"] == c) & (ytd_q3_data["period_start"] == ps)] if not ytd_q3_data.empty else pd.DataFrame()
        if not mq3.empty and not pd.isna(mq3["numeric_value"].iloc[0]):
            derived.append({"concept": c, "period_end": pe, "fiscal_period": "Q4", "fiscal_year": fy_yr, "quarter_label": _period_end_to_quarter_label(pe, "Q4"), "numeric_value": fv - mq3["numeric_value"].iloc[0], "unit": unit, "period_type": "duration", "statement_type": statement_type})
    
    if derived:
        res = pd.concat([existing_df, pd.DataFrame(derived)], ignore_index=True).dropna(subset=["numeric_value"])
        return res.sort_values(["concept", "quarter_label", "fiscal_year"], ascending=[True, True, False]).drop_duplicates(["concept", "quarter_label"], keep="first")
    return existing_df

def process_income(all_facts_df: pd.DataFrame, bp: pd.DataFrame, num_quarters: int) -> pd.DataFrame:
    data_c = bp[~bp["is_abstract"]]["concept"].tolist()
    avail_c = set(all_facts_df["concept"].unique())
    c_list = [c for c in data_c if c in avail_c]
    if not c_list: return pd.DataFrame()
    
    df = all_facts_df[(all_facts_df["concept"].isin(c_list)) & (all_facts_df["fiscal_period"].isin({"Q1","Q2","Q3","Q4"})) & (all_facts_df["period_type"] == "duration")].copy()
    if not df.empty:
        df["period_start_dt"] = pd.to_datetime(df["period_start"], errors="coerce")
        df["period_end_dt"] = pd.to_datetime(df["period_end"], errors="coerce")
        df["calc_duration"] = (df["period_end_dt"] - df["period_start_dt"]).dt.days
        df = df[df["calc_duration"] <= MAX_QUARTERLY_DURATION_DAYS]
        df = df.sort_values(["concept", "period_end", "fiscal_period", "fiscal_year"], ascending=[True, True, True, False]).drop_duplicates(["concept", "period_end", "fiscal_period"], keep="first")
    else:
        df = pd.DataFrame(columns=["concept", "label", "period_start", "period_end", "fiscal_period", "fiscal_year", "numeric_value", "unit", "period_type"])
        
    df["quarter_label"] = df.apply(lambda r: _period_end_to_quarter_label(r["period_end"], r["fiscal_period"]), axis=1) if not df.empty else []
    df = _derive_q4_from_fy(all_facts_df, c_list, df, "income")
    if df.empty: return df
    df = _enrich_with_blueprint(df, bp)
    df["statement_type"] = "income"
    return _limit_quarters(df, num_quarters)

def process_balance(all_facts_df: pd.DataFrame, bp: pd.DataFrame, num_quarters: int) -> pd.DataFrame:
    data_c = bp[~bp["is_abstract"]]["concept"].tolist()
    c_list = [c for c in data_c if c in set(all_facts_df["concept"].unique())]
    if not c_list: return pd.DataFrame()
    
    df = all_facts_df[(all_facts_df["concept"].isin(c_list)) & (all_facts_df["fiscal_period"].isin({"Q1","Q2","Q3","Q4","FY"})) & (all_facts_df["period_type"] == "instant")].copy()
    if df.empty: return df
    df = df.sort_values(["concept", "period_end", "fiscal_period"], ascending=[True, True, True]).drop_duplicates(["concept", "period_end"], keep="first")
    df.loc[df["fiscal_period"] == "FY", "fiscal_period"] = "Q4"
    df = _enrich_with_blueprint(df, bp)
    df["quarter_label"] = df.apply(lambda r: _period_end_to_quarter_label(r["period_end"], r["fiscal_period"]), axis=1)
    df["statement_type"] = "balance"
    return _limit_quarters(df, num_quarters)

def process_cashflow(all_facts_df: pd.DataFrame, bp: pd.DataFrame, num_quarters: int) -> pd.DataFrame:
    data_c = bp[~bp["is_abstract"]]["concept"].tolist()
    c_list = [c for c in data_c if c in set(all_facts_df["concept"].unique())]
    if not c_list: return pd.DataFrame()
    
    df = all_facts_df[(all_facts_df["concept"].isin(c_list)) & (all_facts_df["fiscal_period"].isin({"Q1","Q2","Q3","Q4","FY"})) & (all_facts_df["period_type"] == "duration")].copy()
    if df.empty: return df
    df["period_start_dt"] = pd.to_datetime(df["period_start"], errors="coerce")
    df["period_end_dt"] = pd.to_datetime(df["period_end"], errors="coerce")
    df["calc_duration"] = (df["period_end_dt"] - df["period_start_dt"]).dt.days
    df = df.sort_values(["concept", "period_start", "fiscal_period", "calc_duration"], ascending=[True, True, True, False]).drop_duplicates(["concept", "period_start", "fiscal_period"], keep="first")
    
    derived = []
    for c in df["concept"].unique():
        cd = df[df["concept"] == c]
        for ps in cd["period_start"].unique():
            fg = cd[cd["period_start"] == ps]
            q1, q2, q3, fy = fg[fg["fiscal_period"] == "Q1"], fg[fg["fiscal_period"] == "Q2"], fg[fg["fiscal_period"] == "Q3"], fg[fg["fiscal_period"] == "FY"]
            q1v = q1["numeric_value"].iloc[0] if len(q1)>0 else None
            q2v = q2["numeric_value"].iloc[0] if len(q2)>0 else None
            q3v = q3["numeric_value"].iloc[0] if len(q3)>0 else None
            fyv = fy["numeric_value"].iloc[0] if len(fy)>0 else None
            f_yr, unit = fg["fiscal_year"].iloc[0], fg["unit"].iloc[0] if "unit" in fg.columns else "USD"
            
            if q1v is not None and pd.notna(q1v): derived.append({"concept": c, "period_end": q1["period_end"].iloc[0], "fiscal_period": "Q1", "fiscal_year": f_yr, "quarter_label": _period_end_to_quarter_label(q1["period_end"].iloc[0], "Q1"), "numeric_value": q1v, "unit": unit, "period_type": "duration", "statement_type": "cashflow"})
            if q2v is not None and pd.notna(q2v) and q1v is not None and pd.notna(q1v): derived.append({"concept": c, "period_end": q2["period_end"].iloc[0], "fiscal_period": "Q2", "fiscal_year": f_yr, "quarter_label": _period_end_to_quarter_label(q2["period_end"].iloc[0], "Q2"), "numeric_value": q2v - q1v, "unit": unit, "period_type": "duration", "statement_type": "cashflow"})
            if q3v is not None and pd.notna(q3v) and q2v is not None and pd.notna(q2v): derived.append({"concept": c, "period_end": q3["period_end"].iloc[0], "fiscal_period": "Q3", "fiscal_year": f_yr, "quarter_label": _period_end_to_quarter_label(q3["period_end"].iloc[0], "Q3"), "numeric_value": q3v - q2v, "unit": unit, "period_type": "duration", "statement_type": "cashflow"})
            if fyv is not None and pd.notna(fyv) and q3v is not None and pd.notna(q3v): derived.append({"concept": c, "period_end": fy["period_end"].iloc[0], "fiscal_period": "Q4", "fiscal_year": f_yr, "quarter_label": _period_end_to_quarter_label(fy["period_end"].iloc[0], "Q4"), "numeric_value": fyv - q3v, "unit": unit, "period_type": "duration", "statement_type": "cashflow"})
    
    if not derived: return pd.DataFrame()
    res = pd.DataFrame(derived).dropna(subset=["numeric_value"]).sort_values(["concept", "quarter_label", "fiscal_year"], ascending=[True, True, False]).drop_duplicates(["concept", "quarter_label"], keep="first")
    res = _enrich_with_blueprint(res, bp)
    return _limit_quarters(res, num_quarters)

def upsert_quarterly_records(records: List[Dict[str, Any]]) -> int:
    if not records: return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    columns = ["ticker", "statement_type", "concept", "label", "quarter", "period_end", "fiscal_period", "fiscal_year", "numeric_value", "unit", "period_type", "depth", "is_total", "concept_order", "content_hash", "extracted_at", "created_at", "updated_at"]
    
    sql = f"""INSERT INTO sf_quarterly_facts ({', '.join(columns)}) VALUES ({', '.join(['?']*len(columns))})
              ON CONFLICT(ticker, statement_type, concept, quarter) DO UPDATE SET 
              label=excluded.label, period_end=excluded.period_end, fiscal_period=excluded.fiscal_period, fiscal_year=excluded.fiscal_year, numeric_value=excluded.numeric_value, unit=excluded.unit, period_type=excluded.period_type, depth=excluded.depth, is_total=excluded.is_total, concept_order=excluded.concept_order, content_hash=excluded.content_hash, updated_at=CURRENT_TIMESTAMP"""
    
    batch_size = 500
    for i in range(0, len(records), batch_size):
        chunk = records[i:i+batch_size]
        statements = []
        for r in chunk:
            r["concept"] = normalize_concept(r.get("concept"))
            r["quarter"] = r.get("quarter_label", "")
            r["numeric_value"] = str(r.get("numeric_value", ""))
            r["content_hash"] = compute_fact_hash(r)
            r["extracted_at"] = r.get("extracted_at", now_iso)
            r["created_at"] = r.get("created_at", now_iso)
        enqueue_transaction(statements)
        
    cloud_batch_size = 5000
    for i in range(0, len(records), cloud_batch_size):
        chunk = records[i:i+cloud_batch_size]
        try:
            # Composite primary key for Snowflake MERGE
            enqueue_cloud_write_batch("sf_quarterly_facts", chunk, pk_col=["ticker", "statement_type", "concept", "quarter"])
        except Exception as e:
            log.dual_log(tag="StockFin:Cloud:BatchFailed", level="WARNING", message=f"Cloud batch write failed: {e}", payload={"batch_size": len(chunk), "error": str(e)[:200]})
    return len(records)

def extract_and_persist(ticker: str, num_quarters: int, refresh: bool, job_id: str | None = None) -> Dict[str, Any]:
    from edgar import set_identity, Company
    import config
    from database.connection import DatabaseManager
    from utils.metadata_helpers import make_metadata
    from database.job_queue import add_job_item, update_item_status
    
    ticker = ticker.upper()
    set_identity(getattr(config, "EDGAR_IDENTITY", "analyst@research.com"))
    edgar_limiter.wait()
    
    conn = DatabaseManager.get_read_connection()
    if refresh:
        from database.writer import enqueue_write
        enqueue_write("DELETE FROM sf_quarterly_facts WHERE ticker = ?", (ticker,))
        enqueue_cloud_delete("sf_quarterly_facts", {"ticker": ticker}, pk_col=["ticker"])
        
    company = Company(ticker)
    now_iso = datetime.now(timezone.utc).isoformat()
    enqueue_transaction([("INSERT OR IGNORE INTO sf_tickers (ticker, company_name, cik) VALUES (?, ?, ?)", (ticker, company.name, company.cik))])
    enqueue_cloud_write_batch("sf_tickers", [{"ticker": ticker, "company_name": company.name, "cik": company.cik, "created_at": now_iso, "updated_at": now_iso}], pk_col="ticker")

    facts = company.get_facts()
    all_facts_df = facts.to_dataframe()
    blueprints = {s: get_statement_blueprint(facts, s) for s in ["income", "balance", "cashflow"]}
    
    processors = {"income": process_income, "balance": process_balance, "cashflow": process_cashflow}
    results = {}
    
    for stype, func in processors.items():
        meta = make_metadata("extract", f"{ticker}|{stype}")
        if job_id: add_job_item(job_id, meta, "{}")
        try:
            df = func(all_facts_df, blueprints[stype], num_quarters)
            if not df.empty:
                df["ticker"] = ticker
                records = df.to_dict(orient="records")
                count = upsert_quarterly_records(records)
                results[stype] = count
                if job_id: update_item_status(job_id, meta, "COMPLETED", json.dumps({"count": count}))
            else:
                if job_id: update_item_status(job_id, meta, "COMPLETED", json.dumps({"count": 0}))
        except Exception as e:
            if job_id: update_item_status(job_id, meta, "FAILED", json.dumps({"error": str(e)}))
            log.dual_log(tag="StockFin:Extract:Error", message=f"Extraction failed for {stype}", level="ERROR", payload={"error": str(e)})

    return {"ticker": ticker, "company": company.name, "persisted": results}
