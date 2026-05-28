# tools/stock_notes/detail_manager.py
import json
import re
from typing import Optional, Tuple, List, Dict, Any

from database.connection import DatabaseManager
from database.writer import enqueue_write

TABLE_PREFIX = "sn_dt_"

def get_full_table_name(ticker: str, detail_table_name: str) -> str:
    ticker_clean = re.sub(r'[^a-z0-9]', '_', ticker.lower())
    name_clean = re.sub(r'[^a-z0-9]', '_', detail_table_name.lower())
    full = f"{TABLE_PREFIX}{ticker_clean}_{name_clean}"
    return full[:80]

def sanitize_column_name(name: str) -> str:
    name = name.strip().replace('\n', ' ').replace('\r', '')
    name = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    return re.sub(r'_+', '_', name)

def ensure_detail_table(ticker: str, detail_table_name: str, columns: List[str]) -> str:
    table_name = get_full_table_name(ticker, detail_table_name)
    conn = DatabaseManager.get_read_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    exists = cursor.fetchone() is not None
    
    receipt = None
    if not exists:
        col_defs = [
            "_row_id INTEGER PRIMARY KEY AUTOINCREMENT",
            "_quarter INTEGER NOT NULL",
            "_year INTEGER NOT NULL",
            "_quarter_label TEXT NOT NULL DEFAULT ''",
            "_quarterly_status TEXT NOT NULL DEFAULT ''",
            "_source_accession_no TEXT NOT NULL DEFAULT ''",
            "_source_note_number INTEGER NOT NULL DEFAULT 0",
            "_extracted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
            "label TEXT NOT NULL DEFAULT ''"
        ]
        for col in columns:
            if col.lower() == "label": continue
            safe_col = sanitize_column_name(col)
            col_defs.append(f'"{safe_col}" TEXT')
        
        ddl = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({", ".join(col_defs)})'
        idx_ddl = f'CREATE INDEX IF NOT EXISTS "idx_{table_name}_quarter" ON "{table_name}" (_quarter, _year)'
        enqueue_write(ddl)
        receipt = enqueue_write(idx_ddl, track=True)
    else:
        cursor = conn.execute(f'PRAGMA table_info("{table_name}")')
        existing_cols = {row[1] for row in cursor.fetchall()}
        for col in columns:
            safe_col = sanitize_column_name(col)
            if safe_col not in existing_cols and col.lower() != "label":
                alter_ddl = f'ALTER TABLE "{table_name}" ADD COLUMN "{safe_col}" TEXT'
                receipt = enqueue_write(alter_ddl, track=True)
    
    if receipt:
        receipt.wait(timeout=15.0)
    
    all_cols = ["label"] + [c for c in columns if c.lower() != "label"]
    enqueue_write(
        "UPDATE sn_detail_registry SET column_schema = ?, updated_at = CURRENT_TIMESTAMP WHERE ticker = ? AND detail_table_name = ?",
        (json.dumps(all_cols), ticker, detail_table_name)
    )
    return table_name

def upsert_detail_records(
    ticker: str, detail_table_name: str, records: List[Dict[str, Any]], 
    columns: List[str], quarter: int, year: int, quarterly_status: str, 
    accession_no: str, note_number: int
) -> int:
    if not records: return 0
    table_name = ensure_detail_table(ticker, detail_table_name, columns)
    
    conn = DatabaseManager.get_read_connection()
    cursor = conn.execute(f'PRAGMA table_info("{table_name}")')
    table_cols = [row[1] for row in cursor.fetchall()]
    
    upserted = 0
    for record in records:
        cols = ["_quarter", "_year", "_quarter_label", "_quarterly_status", "_source_accession_no", "_source_note_number"]
        vals = [quarter, year, f"{year}-Q{quarter}", quarterly_status, accession_no, note_number]
        
        for rec_col in columns:
            safe_col = sanitize_column_name(rec_col)
            if safe_col in table_cols:
                cols.append(f'"{safe_col}"')
                val = record.get(rec_col, "")
                vals.append("" if val is None or (isinstance(val, float) and str(val) == "nan") else str(val))
        
        placeholders = ", ".join(["?"] * len(cols))
        col_str = ", ".join(cols)
        sql = f'INSERT OR REPLACE INTO "{table_name}" ({col_str}) VALUES ({placeholders})'
        enqueue_write(sql, tuple(vals))
        upserted += 1
        
    enqueue_write(
        f'UPDATE sn_detail_registry SET row_count = (SELECT COUNT(*) FROM "{table_name}"), updated_at = CURRENT_TIMESTAMP WHERE ticker = ? AND detail_table_name = ?',
        (ticker, detail_table_name)
    )
    return upserted

def query_detail_table(
    ticker: str, detail_table_name: str, start_date: Optional[str] = None, 
    end_date: Optional[str] = None, fiscal_year_end_month: int = 12, max_quarters: int = 12
) -> Tuple[str, List[Dict[str, Any]]]:
    from tools.stock_notes.fiscal import parse_quarter_date, fiscal_quarter_from_period_end, quarter_date_range
    full_table_name = get_full_table_name(ticker, detail_table_name)
    conn = DatabaseManager.get_read_connection()
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (full_table_name,)).fetchone():
        return (full_table_name, [])
        
    where_parts, params = [], []
    if start_date and end_date:
        quarters = quarter_date_range(start_date, end_date, fiscal_year_end_month, max_quarters)
        if quarters:
            q_conditions = []
            for q, y in quarters:
                q_conditions.append("(_quarter = ? AND _year = ?)")
                params.extend([q, y])
            where_parts.append(f"({' OR '.join(q_conditions)})")
    elif start_date or end_date:
        if start_date:
            y, m = parse_quarter_date(start_date)
            if y > 0:
                q, fy = fiscal_quarter_from_period_end(__import__('datetime').date(y, m, 1), fiscal_year_end_month)
                where_parts.append("(_year > ? OR (_year = ? AND _quarter >= ?))")
                params.extend([fy, fy, q])
        if end_date:
            y, m = parse_quarter_date(end_date)
            if y > 0:
                q, fy = fiscal_quarter_from_period_end(__import__('datetime').date(y, m, 1), fiscal_year_end_month)
                where_parts.append("(_year < ? OR (_year = ? AND _quarter <= ?))")
                params.extend([fy, fy, q])
                
    where_sql = " AND ".join(where_parts) if where_parts else "1=1"
    sql = f'SELECT * FROM "{full_table_name}" WHERE {where_sql} ORDER BY _year DESC, _quarter DESC, label LIMIT {max_quarters * 50}'
    
    cursor = conn.execute(sql, tuple(params))
    columns = [desc[0] for desc in cursor.description]
    records = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return (full_table_name, records)

def format_as_markdown_table(records: List[Dict[str, Any]], table_name: str) -> str:
    if not records: return f"**{table_name}**: (no data found for selected date range)"
    all_cols = list(records[0].keys())
    display_cols = [c for c in all_cols if not c.startswith('_')] or all_cols
    
    lines = ["| " + " | ".join(display_cols) + " |", "| " + " | ".join("---" for _ in display_cols) + " |"]
    for record in records:
        lines.append("| " + " | ".join(str(record.get(col, "")) for col in display_cols) + " |")
    return "\n".join(lines)

def list_available_detail_tables(ticker: str) -> List[Dict[str, Any]]:
    conn = DatabaseManager.get_read_connection()
    cursor = conn.execute(
        "SELECT detail_table_name, source_title, role_or_type, column_schema, row_count, quarter, year, quarterly_status FROM sn_detail_registry WHERE ticker = ? ORDER BY detail_table_name, year DESC, quarter DESC",
        (ticker,)
    )
    return [{
        "detail_table_name": r[0], "source_title": r[1], "role_or_type": r[2],
        "columns": json.loads(r[3]) if r[3] else [], "row_count": r[4], 
        "quarter": r[5], "year": r[6], "quarterly_status": r[7]
    } for r in cursor.fetchall()]

def register_detail_table(
    ticker: str, detail_table_name: str, source_title: str, source_note_number: int, 
    source_accession_no: str, role_or_type: str, columns: List[str], row_count: int, 
    quarter: int, year: int, quarterly_status: str
):
    enqueue_write(
        """INSERT OR REPLACE INTO sn_detail_registry
           (ticker, detail_table_name, source_title, source_note_number, source_accession_no, role_or_type, column_schema, row_count, quarter, year, quarterly_status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (ticker, detail_table_name, source_title, source_note_number, source_accession_no, role_or_type, json.dumps(columns), row_count, quarter, year, quarterly_status)
    )
