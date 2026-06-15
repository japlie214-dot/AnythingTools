# tools/stock_financials/tool.py
import json
import sqlite3
import pandas as pd
from typing import Any
from tools.base import BaseTool
from utils.logger import get_dual_logger
from utils.context_helpers import to_thread_with_context
from utils.artifact_manager import write_artifact
from .models import StockFinancialsInput
from .extractor import extract_and_persist
from .query import query_facts, format_results_as_markdown

log = get_dual_logger(__name__)

class StockFinancialsTool(BaseTool):
    name = "stock_financials"
    INPUT_MODEL = StockFinancialsInput

    def is_resumable(self, args: dict[str, Any]) -> bool:
        return True

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        cmd = args.get("command", "").lower().strip()
        job_id = kwargs.get("job_id", "")
        inst = args.get("instructions", {})
        ticker = (inst.get("ticker") or "").upper().strip()
        
        def _fail(summary: str, next_steps: str) -> str:
            return json.dumps({"_callback_format": "structured", "tool_name": self.name, "status": "FAILED", "summary": summary, "status_overrides": {"FAILED": {"description": "Stock Financials failed", "next_steps": next_steps, "rerunnable": True}}}, ensure_ascii=False)

        def _success(summary: str, details: dict, artifacts: list = None) -> str:
            return json.dumps({"_callback_format": "structured", "tool_name": self.name, "status": "COMPLETED", "summary": summary, "details": details, "artifacts": artifacts or []}, ensure_ascii=False)

        if not ticker: return _fail("Missing ticker", "Provide a ticker symbol.")

        if cmd == "extract":
            quarters = min(int(inst.get("quarters", 8)), 40)
            refresh = bool(inst.get("refresh", False))
            try:
                from database.connection import DatabaseManager
                conn = DatabaseManager.get_read_connection()
                existing = conn.execute("SELECT COUNT(DISTINCT quarter) FROM sf_quarterly_facts WHERE ticker=?", (ticker,)).fetchone()[0]
                if existing >= quarters and not refresh:
                    return _success(f"{ticker} already has {existing} quarters in cache. Pass 'refresh: true' to force overwrite.", {"ticker": ticker, "quarters_cached": existing})
                
                await to_thread_with_context(extract_and_persist, ticker, quarters, refresh, job_id)
                # Generate CSV Artifact (Artifact-as-Receipt rule)
                conn = DatabaseManager.get_read_connection()
                conn.row_factory = sqlite3.Row
                all_data = [dict(r) for r in conn.execute("SELECT * FROM sf_quarterly_facts WHERE ticker=? ORDER BY statement_type, concept_order, quarter DESC", (ticker,)).fetchall()]
                if all_data:
                    df = pd.DataFrame(all_data)
                    csv_path = write_artifact(self.name, job_id, f"{ticker}_financials", "csv", df.to_csv(index=False))
                    return _success(f"Extraction complete for {ticker} ({quarters} quarters).", {"ticker": ticker}, [{"filename": csv_path.name, "type": "file", "description": "Tabular CSV Export"}])
                return _success(f"No data extracted for {ticker}.", {"ticker": ticker})
            except Exception as e:
                return _fail(f"Extraction failed: {e}", "Verify ticker and EDGAR connectivity.")

        elif cmd == "query":
            stmt_type = inst.get("statement_type", "income")
            concept = inst.get("concept")
            limit = min(int(inst.get("limit", 100)), 500)
            records = await to_thread_with_context(query_facts, ticker, stmt_type, concept, inst.get("start_quarter"), inst.get("end_quarter"), limit)
            
            if not records:
                return _fail(f"No records found for {ticker} {stmt_type}.", "Run extract command first or check your spelling.")
            
            md_table = format_results_as_markdown(records)
            art_path = write_artifact(self.name, job_id, f"query_{ticker}_{stmt_type}", "md", md_table)
            return _success(f"Queried {len(records)} facts. See artifact for details.\n\n{md_table[:2000]}...", {"rows": len(records)}, [{"filename": art_path.name, "type": "file", "description": "Markdown Results Table"}])

        elif cmd == "status":
            from database.connection import DatabaseManager
            conn = DatabaseManager.get_read_connection()
            rows = conn.execute("SELECT statement_type, COUNT(DISTINCT quarter) as q_count, MAX(quarter) as latest FROM sf_quarterly_facts WHERE ticker=? GROUP BY statement_type", (ticker,)).fetchall()
            lines = [f"# Cache Status for {ticker}"]
            for r in rows:
                lines.append(f"- **{r[0]}**: {r[1]} quarters (latest: {r[2]})")
            return _success("\n".join(lines) if rows else f"No cache for {ticker}.", {"ticker": ticker, "status": [dict(r) for r in rows]})
            
        return _fail("Invalid command", "Use extract, query, or status.")
