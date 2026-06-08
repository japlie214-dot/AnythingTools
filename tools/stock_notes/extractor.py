# tools/stock_notes/extractor.py
import json
import re
from datetime import datetime
from typing import Optional, List, Dict, Any

from utils.edgar_rate_limiter import edgar_limiter
from database.writer import enqueue_write
from database.job_queue import add_job_item, update_item_status
from utils.metadata_helpers import make_metadata
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)
APPROVED_FORMS = {"10-K", "10-Q", "20-F", "6-K"}

def set_edgar_identity():
    import os
    from edgar import set_identity
    identity = os.environ.get("EDGAR_IDENTITY")
    if identity: set_identity(identity)

def discover_filings(ticker: str, form_types: Optional[List[str]] = None, limit: int = 40) -> List[Dict[str, Any]]:
    from edgar import Company
    from tools.stock_notes.fiscal import get_fiscal_year_end_month, fiscal_quarter_from_period_end
    
    valid_forms = [f.strip().upper() for f in (form_types or APPROVED_FORMS) if f.strip().upper() in APPROVED_FORMS]
    if not valid_forms: return []
    
    set_edgar_identity()
    edgar_limiter.wait()
    company = Company(ticker)
    fy_month = get_fiscal_year_end_month(ticker, company=company)
    
    results = []
    for form in valid_forms:
        try:
            edgar_limiter.wait()
            filings = company.get_filings(form=form, amendments=False)
            if not filings: continue
            
            for count, f in enumerate(filings):
                if count >= 20: break
                
                period = str(getattr(f, "period_of_report", "") or getattr(f, "period_of_report_date", "") or "")
                quarter, year = 0, 0
                if period:
                    try:
                        quarter, year = fiscal_quarter_from_period_end(datetime.strptime(period[:10], "%Y-%m-%d").date(), fy_month)
                    except ValueError: pass
                
                results.append({
                    "ticker": ticker.upper(), "company_name": company.name, "cik": company.cik, "form": f.form,
                    "filing_date": str(f.filing_date), "accession_no": f.accession_no, "period_of_report": period,
                    "quarter": quarter, "year": year, "fiscal_year_end_month": fy_month
                })
        except Exception as e:
            log.dual_log(tag="StockNotes:Discover:Error", message=f"Discovery failed for {form}", level="WARNING", payload={"error": str(e)})
            
    results.sort(key=lambda x: x.get("filing_date", ""), reverse=True)
    return results[:limit]

def extract_and_persist_filing(accession_no: str, ticker: str = "", form: str = "", job_id: str = "") -> Dict[str, Any]:
    from edgar import Company, find as edgar_find
    from tools.stock_notes.fiscal import get_fiscal_year_end_month, fiscal_quarter_from_period_end
    from tools.stock_notes.detail_manager import upsert_detail_records, register_detail_table
    from database.stock_notes.store import get_filing_store
    
    set_edgar_identity()
    edgar_limiter.wait()
    filing = edgar_find(search_id=accession_no)
    
    cik = getattr(filing, 'cik', 0)
    if not ticker and cik:
        try:
            edgar_limiter.wait()
            comp = Company(cik)
            ticker = comp.tickers[0].upper() if hasattr(comp, 'tickers') and comp.tickers else str(cik)
        except Exception: ticker = str(cik)
    elif not ticker: ticker = "UNKNOWN"
    
    if not form: form = filing.form
    
    company = Company(cik) if cik else Company(ticker)
    fy_month = get_fiscal_year_end_month(ticker, company=company)
    
    obj = filing.obj()
    period = str(getattr(obj, "period_of_report", ""))
    quarter, year = 0, 0
    if period:
        try:
            quarter, year = fiscal_quarter_from_period_end(datetime.strptime(period[:10], "%Y-%m-%d").date(), fy_month)
        except ValueError: pass
        
    filing_id = f"{ticker}|{form}|{accession_no}"
    enqueue_write(
        """INSERT OR REPLACE INTO sn_filings (filing_id, ticker, form, filing_date, accession_no, period_of_report, company_name, cik, fiscal_year_end_month, quarter, year, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (filing_id, ticker, form, str(filing.filing_date), accession_no, period, str(getattr(filing, 'company', "Unknown")), cik, fy_month, quarter, year)
    )
    try:
        from database.backup.writer.cloud_writer import enqueue_cloud_write
        enqueue_cloud_write("sn_filings", {
            "filing_id": filing_id, "ticker": ticker, "form": form, "filing_date": str(filing.filing_date),
            "accession_no": accession_no, "period_of_report": period, "company_name": str(getattr(filing, 'company', "Unknown")),
            "cik": cik, "fiscal_year_end_month": fy_month, "quarter": quarter, "year": year
        }, pk_col="filing_id")
    except Exception:
        pass
    
    if not hasattr(obj, "notes") or not obj.notes:
        return {"filing_id": filing_id, "ticker": ticker, "accession_no": accession_no, "note_count": 0, "detail_table_count": 0}
        
    q_status = "direct" if form in ("10-Q", "6-K") else ("from_annual_filing" if form in ("10-K", "20-F") else "unknown")
    total_detail_tables = 0
    
    # Store complete payload via JSON/bin archive (Backup Integration)
    filing_payload = {"notes": []}

    for note in list(obj.notes):
        note_meta = make_metadata("extract_note", f"{filing_id}|N{note.number}")
        if job_id: add_job_item(job_id, note_meta, "{}")
        
        try:
            note_id = f"{filing_id}|N{note.number}"
            narrative = getattr(note, "text", "") or ""
            
            note_payload = {"note_number": note.number, "title": note.title, "narrative": narrative, "tables": []}
            
            if note.tables:
                for ti, t in enumerate(note.tables):
                    try:
                        df = t.to_dataframe()
                        if df is not None and not df.empty:
                            table_title = getattr(t.render(), "title", "") or f"Table {ti}"
                            dt_name = re.sub(r'[^a-zA-Z0-9]', '_', table_title or f"Note{note.number}_T{ti}")[:50].strip('_')
                            
                            count = upsert_detail_records(ticker, dt_name, df.to_dict(orient="records"), list(df.columns), quarter, year, q_status, accession_no, note.number)
                            register_detail_table(ticker, dt_name, table_title, note.number, accession_no, getattr(t, "role_or_type", ""), list(df.columns), count, quarter, year, q_status)
                            total_detail_tables += 1
                            note_payload["tables"].append({"name": dt_name, "title": table_title, "rows": count, "data": df.to_dict(orient="records")})
                    except Exception: pass
            
            enqueue_write(
                """INSERT OR REPLACE INTO sn_notes (note_id, filing_id, ticker, form, accession_no, note_number, title, narrative_text, quarter, year, quarterly_status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (note_id, filing_id, ticker, form, accession_no, note.number, note.title, narrative, quarter, year, q_status)
            )
            try:
                from database.backup.writer.cloud_writer import enqueue_cloud_write
                enqueue_cloud_write("sn_notes", {
                    "note_id": note_id, "filing_id": filing_id, "ticker": ticker, "form": form, "accession_no": accession_no,
                    "note_number": note.number, "title": note.title, "narrative_text": narrative, "quarter": quarter,
                    "year": year, "quarterly_status": q_status
                }, pk_col="note_id")
            except Exception:
                pass
            filing_payload["notes"].append(note_payload)
            if job_id: update_item_status(job_id, note_meta, "COMPLETED", json.dumps({"tables": len(note_payload["tables"])}))
        except Exception as e:
            if job_id: update_item_status(job_id, note_meta, "FAILED", json.dumps({"error": str(e)}))
            log.dual_log(tag="StockNotes:Extract:NoteError", message=f"Note {note.number} failed", level="WARNING", payload={"error": str(e)})

    # JSON Archive Integration
    try:
        get_filing_store().upsert_filing_payload(accession_no, filing_payload)
    except Exception as e:
        log.dual_log(tag="StockNotes:Store:Error", message=f"FilingStore failed for {accession_no}", level="ERROR", payload={"error": str(e)})

    return {"filing_id": filing_id, "ticker": ticker, "accession_no": accession_no, "note_count": len(obj.notes), "detail_table_count": total_detail_tables}
