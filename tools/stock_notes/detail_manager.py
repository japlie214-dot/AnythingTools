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
    from datetime import datetime, timezone
    
    if not records:
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    for r in records:
        r["extracted_at"] = r.get("extracted_at") or now_iso
        r["created_at"] = r.get("created_at") or now_iso
        r["updated_at"] = r.get("updated_at") or now_iso

    columns = [
        "detail_id", "accession_no", "note_number", "detail_index", "ticker", "form",
        "concept", "label", "standard_concept", "level", "abstract", "dimension",
        "is_breakdown", "dimension_axis", "dimension_member", "dimension_member_label",
        "dimension_label", "balance", "weight", "preferred_sign", "parent_concept",
        "parent_abstract_concept", "period_raw", "period_end_date", "period_type",
        "value", "row_order", "content_hash", "extracted_at", "created_at", "updated_at"
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
        except Exception as e:
            from utils.logger import get_dual_logger
            get_dual_logger(__name__).dual_log(
                tag="StockNotes:Cloud:BatchFailed",
                level="WARNING",
                message=f"Cloud batch write failed for sn_note_details: {e}",
                payload={"batch_size": len(chunk), "error": str(e)[:200]}
            )

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

def delete_filing_data(accession_no: str) -> int:
    from database.writer import enqueue_write
    from database.backup.writer.cloud_writer import enqueue_cloud_delete
    
    conn = DatabaseManager.get_read_connection()
    cursor = conn.execute(
        "SELECT COUNT(*) FROM sn_note_details WHERE accession_no = ?",
        (accession_no,)
    )
    count = cursor.fetchone()[0]
    
    if count > 0:
        enqueue_write(
            "DELETE FROM sn_note_details WHERE accession_no = ?",
            (accession_no,)
        )
        try:
            enqueue_cloud_delete("sn_note_details", accession_no, pk_col="accession_no")
        except Exception as e:
            from utils.logger import get_dual_logger
            get_dual_logger(__name__).dual_log(tag="StockNotes:Cloud:DeleteFailed", level="WARNING", message=f"Cloud delete failed for sn_note_details: {e}", payload={"accession_no": accession_no, "error": str(e)[:200]})
    
    enqueue_write(
        "DELETE FROM sn_detail_registry WHERE source_accession_no = ?",
        (accession_no,)
    )
    try:
        enqueue_cloud_delete("sn_detail_registry", accession_no, pk_col="source_accession_no")
    except Exception as e:
        from utils.logger import get_dual_logger
        get_dual_logger(__name__).dual_log(tag="StockNotes:Cloud:DeleteFailed", level="WARNING", message=f"Cloud delete failed for sn_detail_registry: {e}", payload={"accession_no": accession_no, "error": str(e)[:200]})

    enqueue_write(
        "DELETE FROM sn_notes WHERE accession_no = ?",
        (accession_no,)
    )
    try:
        enqueue_cloud_delete("sn_notes", accession_no, pk_col="accession_no")
    except Exception as e:
        from utils.logger import get_dual_logger
        get_dual_logger(__name__).dual_log(tag="StockNotes:Cloud:DeleteFailed", level="WARNING", message=f"Cloud delete failed for sn_notes: {e}", payload={"accession_no": accession_no, "error": str(e)[:200]})
    
    return count

def build_concept_catalog(ticker: str, accession_no: str, note_number: int) -> list[dict]:
    conn = DatabaseManager.get_read_connection()
    
    cursor = conn.execute("""
        SELECT concept, label, dimension_axis, dimension_member_label,
               COUNT(DISTINCT period_end_date) as period_count,
               MIN(period_end_date) as earliest_period,
               MAX(period_end_date) as latest_period,
               period_type, value
        FROM sn_note_details
        WHERE ticker = ? AND accession_no = ? AND note_number = ?
          AND abstract = 'False' AND value != ''
        GROUP BY concept, dimension_member_label
        ORDER BY concept, dimension_member_label
        LIMIT 50
    """, (ticker, accession_no, note_number))
    
    catalog = []
    for row in cursor.fetchall():
        concept, label, dim_axis, dim_member, pcount, earliest, latest, ptype, value = row
        catalog.append({
            "concept": concept,
            "label": label,
            "dimension_axis": dim_axis or "",
            "dimension_member_label": dim_member or "",
            "period_count": pcount,
            "earliest_period": earliest or "",
            "latest_period": latest or "",
            "period_type": ptype,
            "sample_value": _format_sample_value(value),
        })
    
    return catalog

def _format_sample_value(value: str) -> str:
    try:
        num = float(value)
        if abs(num) >= 1e9:
            return f"{num/1e9:.1f}B"
        elif abs(num) >= 1e6:
            return f"{num/1e6:.1f}M"
        elif abs(num) >= 1e3:
            return f"{num/1e3:.1f}K"
        else:
            return f"{num:.2f}"
    except (ValueError, TypeError):
        return value[:20]

def get_date_range_for_filing(accession_no: str) -> tuple[str, str]:
    conn = DatabaseManager.get_read_connection()
    row = conn.execute(
        "SELECT period_of_report, fiscal_year_end_month FROM sn_filings WHERE accession_no = ?",
        (accession_no,)
    ).fetchone()
    
    if not row or not row[0]:
        return "", ""
    
    period_str = row[0][:10]
    fy_month = row[1] or 12
    
    from datetime import datetime
    try:
        period_date = datetime.strptime(period_str, "%Y-%m-%d")
        end_date = period_date.strftime("%Y-%m")
        start_year = period_date.year - 2
        start_date = f"{start_year:04d}-{fy_month:02d}"
        return start_date, end_date
    except ValueError:
        return "", ""

def register_detail_table(
    ticker: str, detail_table_name: str, source_title: str, source_note_number: int,
    source_accession_no: str, role_or_type: str, unique_concepts: List[str], row_count: int,
    quarter: int, year: int, quarterly_status: str
):
    from tools.stock_notes.tidy_transform import make_registry_id
    from database.backup.writer.cloud_writer import enqueue_cloud_write
    from datetime import datetime, timezone
    from utils.logger import get_dual_logger
    
    log = get_dual_logger(__name__)
    rid = make_registry_id(ticker, detail_table_name, source_accession_no, source_note_number)
    now_iso = datetime.now(timezone.utc).isoformat()
    
    enqueue_write(
        """INSERT OR REPLACE INTO sn_detail_registry
           (registry_id, ticker, detail_table_name, source_title, source_note_number, source_accession_no, role_or_type, available_concepts, tidy_schema_version, row_count, quarter, year, quarterly_status, content_hash, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, '', ?, ?)""",
        (rid, ticker, detail_table_name, source_title, source_note_number, source_accession_no, role_or_type, json.dumps(unique_concepts), row_count, quarter, year, quarterly_status, now_iso, now_iso)
    )
    # Inline dual-write: construct cloud record from same data, avoiding read-back
    try:
        record = {
            "registry_id": rid, "ticker": ticker, "detail_table_name": detail_table_name,
            "source_title": source_title, "source_note_number": source_note_number,
            "source_accession_no": source_accession_no, "role_or_type": role_or_type,
            "available_concepts": json.dumps(unique_concepts), "tidy_schema_version": 1,
            "row_count": row_count, "quarter": quarter, "year": year,
            "quarterly_status": quarterly_status, "content_hash": "",
            "created_at": now_iso, "updated_at": now_iso
        }
        enqueue_cloud_write("sn_detail_registry", record, pk_col="registry_id")
    except Exception as e:
        log.dual_log(tag="StockNotes:Cloud:WriteFailed", level="WARNING",
                     message=f"Cloud write failed for sn_detail_registry: {e}",
                     payload={"registry_id": rid, "error": str(e)[:200]})
