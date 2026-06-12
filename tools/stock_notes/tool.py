# tools/stock_notes/tool.py
import json
from typing import Any
from tools.base import BaseTool
from .models import StockNotesInput
from utils.logger import get_dual_logger
from utils.artifact_manager import write_artifact
from utils.context_helpers import to_thread_with_context
from database.writer import wait_for_writes

log = get_dual_logger(__name__)

class StockNotesTool(BaseTool):
    name = "stock_notes"
    INPUT_MODEL = StockNotesInput
    
    def is_resumable(self, args: dict[str, Any]) -> bool:
        return True
        
    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        cmd = args.get("command", "").lower().strip()
        job_id = kwargs.get("job_id", "")
        
        def _fail(summary: str, next_steps: str) -> str:
            return json.dumps({"_callback_format": "structured", "tool_name": self.name, "status": "FAILED", "summary": summary, "status_overrides": {"FAILED": {"description": "Stock Notes execution failed", "next_steps": next_steps, "rerunnable": True}}}, ensure_ascii=False)
            
        def _success(summary: str, details: dict, artifacts: list = None) -> str:
            return json.dumps({"_callback_format": "structured", "tool_name": self.name, "status": "COMPLETED", "summary": summary, "details": details, "artifacts": artifacts or []}, ensure_ascii=False)

        raw_inst = args.get("instructions", {})
        if isinstance(raw_inst, str):
            try:
                instructions = json.loads(raw_inst)
            except Exception:
                return _fail("Invalid instructions payload", "The instructions parameter must be a valid JSON object.")
        else:
            instructions = raw_inst

        ticker = (instructions.get("ticker") or "").upper().strip()
        forms = instructions.get("forms") or "10-K,10-Q"
        accession_no = (instructions.get("accession_no") or "").strip()
        note_number = instructions.get("note_number")
        concept = (instructions.get("concept") or "").strip()
        start_date = (instructions.get("start_date") or "").strip() or None
        end_date = (instructions.get("end_date") or "").strip() or None

        if cmd == "discover":
            from .extractor import discover_filings
            if not ticker: return _fail("Missing ticker", "Provide a ticker symbol in the instructions payload.")
            form_types = [f.strip() for f in forms.split(",") if f.strip()]
            filings = await to_thread_with_context(discover_filings, ticker, form_types=form_types, limit=40)
            
            if not filings: return _fail(f"No filings found for {ticker}", "Verify the ticker.")
            
            lines = [f"# Filings for {ticker} ({len(filings)} found, newest first)\n"]
            lines.append("| # | Form | Filing Date | Period | Quarter | Accession No |")
            lines.append("|---|------|-------------|--------|---------|--------------|")

            for i, f in enumerate(filings):
                quarter_str = f"Q{f['quarter']} FY{f['year']}" if f.get("quarter") else "N/A"
                lines.append(f"| {i+1} | {f['form']} | {f['filing_date']} | {f.get('period_of_report', 'N/A')} | {quarter_str} | {f['accession_no']} |")
            
            lines.append(f"\nUse `note` command with instructions `{{\"accession_no\": \"<accession_no>\"}}` to list notes.")
            
            return _success("\n".join(lines), {"filings": filings})
            
        elif cmd == "note":
            from .extractor import extract_and_persist_filing
            from database.connection import DatabaseManager
            if not accession_no: return _fail("Missing accession_no", "Provide an accession number in the instructions payload.")
            
            conn = DatabaseManager.get_read_connection()
            if not conn.execute("SELECT 1 FROM sn_filings WHERE accession_no=?", (accession_no,)).fetchone():
                try:
                    await to_thread_with_context(extract_and_persist_filing, accession_no, ticker=ticker, job_id=job_id)
                    await wait_for_writes(timeout=15.0)
                    conn = DatabaseManager.get_read_connection()
                except Exception as e:
                    return _fail(f"Extraction failed: {e}", "Ensure valid accession_no.")
            
            filing_row = conn.execute("SELECT ticker, form, company_name, period_of_report, quarter, year, fiscal_year_end_month FROM sn_filings WHERE accession_no=?", (accession_no,)).fetchone()
            if not filing_row:
                return _fail(f"Filing {accession_no} not found after extraction.", "Try the discover command first.")
            f_ticker, f_form, f_company, f_period, f_quarter, f_year, f_fye = filing_row

            if note_number is None:
                notes = conn.execute("SELECT note_number, title, short_name, table_count, details_count, quarterly_status FROM sn_notes WHERE accession_no=? ORDER BY note_number", (accession_no,)).fetchall()
                if not notes: return _success(f"No notes found in filing {accession_no}.", {"accession_no": accession_no})
                
                lines = [f"# Notes in {f_form} Filing: {f_company} ({f_ticker})", f"**Accession:** {accession_no} | **Period:** {f_period} | Q{f_quarter} FY{f_year}\n"]
                lines.append("| Note# | Title | Tables | Details | Q Status |")
                lines.append("|-------|-------|--------|---------|----------|")
                for n in notes:
                    lines.append(f"| {n[0]} | {n[1]} | {n[3]} | {n[4]} | {n[5]} |")
                return _success("\n".join(lines), {"notes_count": len(notes), "accession_no": accession_no})
            
            note_row = conn.execute("SELECT note_number, title, short_name, narrative_text, expands, expands_statements, table_count, details_count, quarterly_status FROM sn_notes WHERE accession_no=? AND note_number=?", (accession_no, note_number)).fetchone()
            if not note_row: return _fail(f"Note {note_number} not found", "Check available notes.")
            
            (n_num, n_title, n_short, n_narrative, n_expands, n_expands_stmts, n_tbl_count, n_dt_count, n_q_status) = note_row
            dts = conn.execute("SELECT detail_table_name, source_title, role_or_type, available_concepts FROM sn_detail_registry WHERE source_accession_no=? AND source_note_number=?", (accession_no, note_number)).fetchall()
            
            lines = [f"# Note {n_num}: {n_title}", f"**Company:** {f_company} ({f_ticker})", f"**Accession:** {accession_no} | **Form:** {f_form}", f"**Quarter:** Q{f_quarter} FY{f_year} | **Status:** {n_q_status}", f"**Tables:** {n_tbl_count} | **Details:** {n_dt_count} | **Detail Tables:** {len(dts)}"]
            if n_expands:
                try:
                    expands = json.loads(n_expands)
                    if expands: lines.append(f"**Expands:** {', '.join(str(e) for e in expands[:5])}")
                except Exception: pass

            artifacts = []
            if n_narrative:
                display_narrative = n_narrative if len(n_narrative) <= 200000 else n_narrative[:200000] + "\n\n...[Truncated. See Artifact for full text]."
                lines.append(f"\n## Narrative\n\n{display_narrative}")
                if len(n_narrative) > 1000:
                    art_path = write_artifact(self.name, job_id, f"note_{n_num}_narrative", "md", n_narrative)
                    artifacts.append({"filename": art_path.name, "type": "file", "description": f"Note {n_num} Narrative"})
            else:
                lines.append("\n*No narrative content available.*")
                
            if dts:
                lines.append(f"\n## Extracted Concepts ({len(dts)} tables)\n")
                for dt_name, dt_title, dt_role, concepts_json in dts:
                    concepts = json.loads(concepts_json) if concepts_json else []
                    lines.append(f"### Table Source: {dt_title or dt_name}\n- **Role:** {dt_role}\n- **Available Concepts:** {', '.join(concepts[:10])}{' ...' if len(concepts) > 10 else ''}\n- Query: `details` command with instructions `{{\"ticker\": \"{f_ticker}\", \"concept\": \"{concepts[0] if concepts else 'example_concept'}\", \"start_date\": \"YYYY-MM\", \"end_date\": \"YYYY-MM\"}}`\n")
            else:
                lines.append("\n*No detail concepts found for this note.*")
                
            return _success("\n".join(lines), {"note_number": note_number}, artifacts)
            
        elif cmd == "details":
            from .detail_manager import query_tidy_table, format_as_markdown_table
            from database.connection import DatabaseManager

            if not ticker or not concept:
                return _fail("Missing ticker or concept.", "Provide both 'ticker' and 'concept' in the instructions payload.")

            try:
                records = query_tidy_table(ticker, concept, start_date, end_date)
            except ValueError as ve:
                return _fail(str(ve), "Use YYYY-MM format (e.g., 2025-03).")

            if not records:
                return _fail(f"No records found for concept '{concept}' on ticker {ticker}", "Adjust date range or extract notes first.")

            md_table = format_as_markdown_table(records, f"Time Series: {concept}")
            art_path = write_artifact(self.name, job_id, "detail_table", "md", md_table)
            
            lines = [f"# Concept Details: {concept} ({ticker})", f"**Records:** {len(records)}"]
            if start_date and end_date: lines.append(f"**Date range:** {start_date} to {end_date}")
            lines.extend(["", md_table])
            return _success("\n".join(lines), {"row_count": len(records)}, [{"filename": art_path.name, "type": "file", "description": "Full Concept Detail Table"}])
            
        return _fail("Invalid command", "Use discover, note, or details.")
