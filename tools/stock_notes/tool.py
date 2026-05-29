# tools/stock_notes/tool.py
import json
from typing import Any
from tools.base import BaseTool
from .models import StockNotesInput
from utils.logger import get_dual_logger
from utils.artifact_manager import write_artifact

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

        if cmd == "discover":
            from .extractor import discover_filings
            ticker = instructions.get("ticker", "").upper()
            if not ticker: return _fail("Missing ticker", "Provide a ticker symbol in the instructions payload.")
            filings = discover_filings(ticker, form_types=(instructions.get("forms") or "10-K,10-Q").split(","))
            
            if not filings: return _fail(f"No filings found for {ticker}", "Verify the ticker.")
            
            form_counts = {}
            for f in filings:
                form_counts[f['form']] = form_counts.get(f['form'], 0) + 1
            count_detail = ', '.join(f"{k}: {v}" for k, v in sorted(form_counts.items()))
            
            lines = [f"Found {len(filings)} filings for {ticker} ({count_detail}). Newest first:"]
            for f in filings[:10]:
                lines.append(f"- {f['form']} | {f['filing_date']} | Accession: {f['accession_no']}")
            lines.append('To explore notes, use command "note" with instructions {"accession_no": "<accession_no>"}')
            
            return _success("\n".join(lines), {"filings": filings})
            
        elif cmd == "note":
            from .extractor import extract_and_persist_filing
            from database.connection import DatabaseManager
            acc_no = instructions.get("accession_no")
            if not acc_no: return _fail("Missing accession_no", "Provide an accession number in the instructions payload.")
            
            conn = DatabaseManager.get_read_connection()
            if not conn.execute("SELECT 1 FROM sn_filings WHERE accession_no=?", (acc_no,)).fetchone():
                try:
                    extract_and_persist_filing(acc_no, ticker=instructions.get("ticker", ""), job_id=job_id)
                    # Re-acquire thread-local connection to trigger generation-based cache validation
                    conn = DatabaseManager.get_read_connection()
                except Exception as e:
                    return _fail(f"Extraction failed: {e}", "Ensure valid accession_no.")
            
            # Fetch Notes
            notes = conn.execute("SELECT note_number, title, narrative_text FROM sn_notes WHERE accession_no=?", (acc_no,)).fetchall()
            
            target_note = instructions.get("note_number")
            if target_note is None:
                lines = [f"Available notes in {acc_no}:"]
                for n in notes:
                    lines.append(f"- Note {n[0]}: {n[1]}")
                return _success("\n".join(lines), {"notes_count": len(notes)})
            
            # Specific Note
            note_row = next((n for n in notes if n[0] == target_note), None)
            if not note_row: return _fail(f"Note {target_note} not found", "Check available notes.")
            
            narrative = note_row[2]
            art_path = write_artifact(self.name, job_id, "narrative", "md", narrative)
            
            # Get detail tables for this note
            dts = conn.execute("SELECT detail_table_name, source_title FROM sn_detail_registry WHERE source_accession_no=? AND source_note_number=?", (acc_no, target_note)).fetchall()
            
            lines = [f"Extracted Note {target_note}: {note_row[1]}", "Narrative saved as artifact."]
            if dts:
                lines.append("\nAvailable Detail Tables (query using the `details` command):")
                for dt in dts: lines.append(f"- {dt[0]} ({dt[1]})")
            else:
                lines.append("\nNo tabular detail tables found for this note.")
                
            return _success("\n".join(lines), {"note_number": target_note}, [{"filename": art_path.name, "type": "file", "description": "Full Note Narrative"}])
            
        elif cmd == "details":
            from .detail_manager import query_detail_table, format_as_markdown_table
            from database.connection import DatabaseManager
            ticker = instructions.get("ticker", "").upper()
            dt_name = instructions.get("detail_table_name")
            if not ticker or not dt_name: return _fail("Missing ticker or detail_table_name", "Both are required in the instructions payload.")
            
            # Fetch target company fiscal year-end month for accurate calendar/fiscal mapping
            conn = DatabaseManager.get_read_connection()
            fye_row = conn.execute("SELECT fiscal_year_end_month FROM sn_filings WHERE ticker = ? LIMIT 1", (ticker,)).fetchone()
            fy_month = fye_row[0] if fye_row else 12
            
            tbl, records = query_detail_table(
                ticker, dt_name, instructions.get("start_date"), instructions.get("end_date"),
                fiscal_year_end_month=fy_month
            )
            if not records: return _fail(f"No records found in {dt_name} for {ticker}", "Adjust date range or check table name.")
            
            md_table = format_as_markdown_table(records, dt_name)
            art_path = write_artifact(self.name, job_id, "detail_table", "md", md_table)
            
            return _success(f"Extracted {len(records)} rows from {dt_name}. Table saved as artifact.", {"row_count": len(records)}, [{"filename": art_path.name, "type": "file", "description": "Full Detail Table"}])
            
        return _fail("Invalid command", "Use discover, note, or details.")
