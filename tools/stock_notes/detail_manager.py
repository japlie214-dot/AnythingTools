# tools/stock_notes/detail_manager.py
import json
import re
from typing import Optional, Tuple, List, Dict, Any

from database.connection import DatabaseManager
from database.writer import enqueue_write

def validate_quarter_date(date_str: str) -> tuple[bool, str]:
    """Validate a quarter date string in YYYY-MM format.
    Returns (is_valid, error_message).
    """
    import re
    if not date_str:
        return True, ""
    if not re.match(r'^\d{4}-(?:0[1-9]|1[0-2])$', date_str):
        return False, f"Invalid date format '{date_str}'. Expected YYYY-MM (e.g., 2025-03)."
    return True, ""


def upsert_tidy_records(records: List[Dict[str, Any]]) -> int:
    from database.writer import enqueue_transaction
    from database.backup.writer.cloud_writer import enqueue_cloud_write_batch
    
    if not records:
        return 0

    columns = [
        "detail_id", "accession_no", "note_number", "detail_index", "ticker", "form",
        "concept", "label", "standard_concept", "level", "abstract", "dimension",
        "is_breakdown", "dimension_axis", "dimension_member", "dimension_member_label",
        "dimension_label", "balance", "weight", "preferred_sign", "parent_concept",
        "parent_abstract_concept", "period_raw", "period_end_date", "period_type",
        "value", "row_order", "content_hash"
    ]
    
    sql = f'''INSERT OR REPLACE INTO sn_note_details ({", ".join(columns)})
              VALUES ({", ".join(["?"] * len(columns))})'''
    
    batch_size = 500
    for i in range(0, len(records), batch_size):
        chunk = records[i:i + batch_size]
        statements = []
        for r in chunk:
            vals = []
            for c in columns:
                val = r.get(c)
                if val is None:
                    if c in ("note_number", "detail_index", "level", "row_order"):
                        vals.append(0)
                    else:
                        vals.append("")
                else:
                    vals.append(val)
            statements.append((sql, tuple(vals)))
        enqueue_transaction(statements)
        
    cloud_batch_size = 5000
    for i in range(0, len(records), cloud_batch_size):
        chunk = records[i:i + cloud_batch_size]
        try:
            enqueue_cloud_write_batch("sn_note_details", chunk, pk_col="detail_id")
        except Exception:
            pass

    return len(records)

def query_tidy_table(
    ticker: str, concept: str, start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> List[Dict[str, Any]]:
    
    if start_date:
        valid, err = validate_quarter_date(start_date)
        if not valid: raise ValueError(err)
    if end_date:
        valid, err = validate_quarter_date(end_date)
        if not valid: raise ValueError(err)

    conn = DatabaseManager.get_read_connection()
    
    where_parts = ["ticker = ?", "concept = ?"]
    params = [ticker, concept]
    
    if start_date:
        where_parts.append("period_end_date >= ?")
        params.append(f"{start_date}-01")
    if end_date:
        where_parts.append("period_end_date <= ?")
        params.append(f"{end_date}-31")
        
    where_sql = " AND ".join(where_parts)
    sql = f'SELECT period_end_date, period_type, value, label, dimension_label FROM sn_note_details WHERE {where_sql} ORDER BY period_end_date DESC LIMIT 500'
    
    cursor = conn.execute(sql, tuple(params))
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]

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
        "SELECT detail_table_name, source_title, role_or_type, available_concepts, row_count, quarter, year, quarterly_status FROM sn_detail_registry WHERE ticker = ? ORDER BY detail_table_name, year DESC, quarter DESC",
        (ticker,)
    )
    return [{
        "detail_table_name": r[0], "source_title": r[1], "role_or_type": r[2],
        "concepts": json.loads(r[3]) if r[3] else [], "row_count": r[4],
        "quarter": r[5], "year": r[6], "quarterly_status": r[7]
    } for r in cursor.fetchall()]

def register_detail_table(
    ticker: str, detail_table_name: str, source_title: str, source_note_number: int,
    source_accession_no: str, role_or_type: str, unique_concepts: List[str], row_count: int,
    quarter: int, year: int, quarterly_status: str
):
    enqueue_write(
        """INSERT OR REPLACE INTO sn_detail_registry
           (ticker, detail_table_name, source_title, source_note_number, source_accession_no, role_or_type, available_concepts, tidy_schema_version, row_count, quarter, year, quarterly_status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (ticker, detail_table_name, source_title, source_note_number, source_accession_no, role_or_type, json.dumps(unique_concepts), row_count, quarter, year, quarterly_status)
    )
    try:
        from database.connection import DatabaseManager
        from database.backup.writer.cloud_writer import enqueue_cloud_write
        conn = DatabaseManager.get_read_connection()
        row = conn.execute("SELECT * FROM sn_detail_registry WHERE ticker = ? AND detail_table_name = ?", (ticker, detail_table_name)).fetchone()
        if row:
            enqueue_cloud_write("sn_detail_registry", dict(row), pk_col="registry_id")
    except Exception:
        pass
