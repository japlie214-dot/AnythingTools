# tools/stock_notes/extractor.py
import json
import re
import hashlib
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
    from database.connection import DatabaseManager
    
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
    filing_content = f"{ticker}|{form}|{accession_no}|{period}|{cik}"
    filing_hash = hashlib.md5(filing_content.encode("utf-8", errors="replace")).hexdigest()
    enqueue_write(
        """INSERT OR REPLACE INTO sn_filings (filing_id, ticker, form, filing_date, accession_no, period_of_report, company_name, cik, fiscal_year_end_month, quarter, year, content_hash, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (filing_id, ticker, form, str(filing.filing_date), accession_no, period, str(getattr(filing, 'company', "Unknown")), cik, fy_month, quarter, year, filing_hash)
    )
    try:
        from database.backup.writer.cloud_writer import enqueue_cloud_write
        now_ts = datetime.utcnow().isoformat()
        enqueue_cloud_write("sn_filings", {
            "filing_id": filing_id, "ticker": ticker, "form": form, "filing_date": str(filing.filing_date),
            "accession_no": accession_no, "period_of_report": period, "company_name": str(getattr(filing, 'company', "Unknown")),
            "cik": cik, "fiscal_year_end_month": fy_month, "quarter": quarter, "year": year, "content_hash": filing_hash,
            "created_at": now_ts, "updated_at": now_ts
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
            narrative_hash = hashlib.md5(narrative.encode("utf-8", errors="replace")).hexdigest() if narrative else ""
            
            expands = getattr(note, "expands", None) or []
            expands_statements = getattr(note, "expands_statements", None) or []
            expands_json = json.dumps(expands) if expands else "[]"
            expands_stmts_json = json.dumps(expands_statements) if expands_statements else "[]"
            
            table_count = len(note.tables) if hasattr(note, "tables") and note.tables else 0
            details_count = len(note.details) if hasattr(note, "details") and note.details else 0
            note_content = f"{note.number}|{note.title}|{narrative_hash}|{table_count}|{details_count}"
            note_hash = hashlib.md5(note_content.encode("utf-8", errors="replace")).hexdigest()
            
            note_payload = {"note_number": note.number, "title": note.title, "narrative": narrative, "tables": [], "details": []}
            
            if hasattr(note, "details") and note.details:
                for di, d in enumerate(note.details):
                    try:
                        df = d.to_dataframe()
                        if df is not None and not df.empty:
                            detail_title = f"Detail_{di}"
                            try:
                                detail_title = str(d)[:100].split("\n")[0] if str(d) else f"Detail {di}"
                            except Exception:
                                pass
                            dt_name = re.sub(r'[^a-zA-Z0-9]', '_', detail_title or f"Note{note.number}_D{di}")[:50].strip('_')
                            
                            count = upsert_detail_records(ticker, dt_name, df.to_dict(orient="records"), list(df.columns), quarter, year, q_status, accession_no, note.number)
                            register_detail_table(ticker, dt_name, detail_title, note.number, accession_no, "detail", list(df.columns), count, quarter, year, q_status)
                            total_detail_tables += 1
                            note_payload["details"].append({"name": dt_name, "title": detail_title, "rows": count})
                    except Exception: pass

            if hasattr(note, "tables") and note.tables:
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
                """INSERT OR REPLACE INTO sn_notes (note_id, filing_id, ticker, form, accession_no, note_number, title, short_name, narrative_text, narrative_hash, expands, expands_statements, table_count, details_count, quarter, year, quarterly_status, version, content_hash, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (note_id, filing_id, ticker, form, accession_no, note.number, note.title,
                 getattr(note, "short_name", note.title) or note.title,
                 narrative, narrative_hash, expands_json, expands_stmts_json,
                 table_count, details_count, quarter, year, q_status, 1, note_hash)
            )
            try:
                from database.backup.writer.cloud_writer import enqueue_cloud_write
                now_ts = datetime.utcnow().isoformat()
                enqueue_cloud_write("sn_notes", {
                    "note_id": note_id, "filing_id": filing_id, "ticker": ticker, "form": form, "accession_no": accession_no,
                    "note_number": note.number, "title": note.title, "short_name": getattr(note, "short_name", note.title) or note.title,
                    "narrative_text": narrative, "narrative_hash": narrative_hash,
                    "expands": expands_json, "expands_statements": expands_stmts_json,
                    "table_count": table_count, "details_count": details_count,
                    "quarter": quarter, "year": year, "quarterly_status": q_status,
                    "version": 1, "content_hash": note_hash,
                    "created_at": now_ts, "updated_at": now_ts
                }, pk_col="note_id")
            except Exception:
                pass
            filing_payload["notes"].append(note_payload)
            if job_id: update_item_status(job_id, note_meta, "COMPLETED", json.dumps({"tables": len(note_payload["tables"]), "details": len(note_payload.get("details", []))}))
        except Exception as e:
            if job_id: update_item_status(job_id, note_meta, "FAILED", json.dumps({"error": str(e)}))
            log.dual_log(tag="StockNotes:Extract:NoteError", message=f"Note {note.number} failed", level="WARNING", payload={"error": str(e)})

    # JSON Archive Integration
    try:
        # Filing store logic is currently handled via direct SQL writes to sn_filings/sn_notes.
        # Payload archiving to a dedicated store is currently unimplemented.
        pass
    except Exception as e:
        log.dual_log(tag="StockNotes:Store:Error", message=f"FilingStore failed for {accession_no}", level="ERROR", payload={"error": str(e)})
        # Filing store logic is currently handled via direct SQL writes to sn_filings/sn_notes.
        # Payload archiving to a dedicated store is currently unimplemented.
        pass
    except Exception as e:
        log.dual_log(tag="StockNotes:Store:Error", message=f"FilingStore failed for {accession_no}", level="ERROR", payload={"error": str(e)})

    return {"filing_id": filing_id, "ticker": ticker, "accession_no": accession_no, "note_count": len(obj.notes), "detail_table_count": total_detail_tables}
