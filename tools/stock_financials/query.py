# tools/stock_financials/query.py
import sqlite3
import pandas as pd
from typing import List, Dict, Any
from database.connection import DatabaseManager

PER_SHARE_UNITS = {"USD per share", "USD/shares"}
SHARE_UNITS = {"shares"}

def format_value(value, unit: str = "USD") -> str:
    if value is None or value == "": return ""
    try: val = float(value)
    except: return str(value)

    if unit in PER_SHARE_UNITS:
        return f"{val:,.2f}" if abs(val) >= 0.01 else f"{val:.4f}"
    if unit in SHARE_UNITS:
        if abs(val) >= 1_000_000_000: return f"{val / 1_000_000_000:,.2f}B"
        elif abs(val) >= 1_000_000: return f"{val / 1_000_000:,.2f}M"
        return f"{val:,.0f}"
    if abs(val) >= 1_000_000_000: return f"${val / 1_000_000_000:,.1f}B"
    elif abs(val) >= 1_000_000: return f"${val / 1_000_000:,.1f}M"
    return f"${val:,.2f}"

def query_facts(ticker: str, statement_type: str, concept: str = None, start_quarter: str = None, end_quarter: str = None, limit: int = 100) -> List[Dict[str, Any]]:
    conn = DatabaseManager.get_read_connection()
    conn.row_factory = sqlite3.Row
    where = ["ticker = ?", "statement_type = ?"]
    params = [ticker.upper(), statement_type.lower()]
    
    if concept:
        where.append("concept = ?")
        params.append(concept.replace(":", "_"))
    if start_quarter:
        where.append("quarter >= ?")
        params.append(start_quarter)
    if end_quarter:
        where.append("quarter <= ?")
        params.append(end_quarter)
        
    sql = f"SELECT * FROM sf_quarterly_facts WHERE {' AND '.join(where)} ORDER BY concept_order ASC, quarter DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]

def format_results_as_markdown(records: List[Dict[str, Any]]) -> str:
    if not records: return "No data found."
    df = pd.DataFrame(records)
    df["formatted_value"] = df.apply(lambda r: format_value(r["numeric_value"], r["unit"]), axis=1)
    
    quarters = sorted(df["quarter"].unique().tolist(), reverse=True)
    lines = ["| Concept | Label | " + " | ".join(quarters) + " |", "|---|---|" + "|".join(["---"]*len(quarters)) + "|"]
    
    for c in df["concept"].unique():
        cd = df[df["concept"] == c]
        label = cd["label"].iloc[0]
        row = [f"`{c}`", label]
        for q in quarters:
            qd = cd[cd["quarter"] == q]
            row.append(qd["formatted_value"].iloc[0] if not qd.empty else "")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)
