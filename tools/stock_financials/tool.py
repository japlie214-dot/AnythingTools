# tools/stock_financials/tool.py
import json
import sqlite3
import pandas as pd
from typing import Any, List, Dict
from tools.base import BaseTool
from utils.logger import get_dual_logger
from utils.context_helpers import to_thread_with_context
from utils.artifact_manager import write_artifact
from .models import StockFinancialsInput
from .extractor import extract_and_persist
from .query import query_facts, query_concepts

log = get_dual_logger(__name__)

class StockFinancialsTool(BaseTool):
    name = "stock_financials"
    INPUT_MODEL = StockFinancialsInput

    def is_resumable(self, args: dict[str, Any]) -> bool:
        return True

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        job_id = kwargs.get("job_id", "")
        try:
            validated = StockFinancialsInput.model_validate(args)
            inst = validated.resolved_instructions()
        except Exception as e:
            return self._fail(f"Invalid input: {e}", "Check the command and instructions shape.")

        cmd = validated.command
        if cmd == "extract": return await self._handle_extract(inst, job_id)
        if cmd == "query": return await self._handle_query(inst, job_id)
        if cmd == "status": return await self._handle_status(inst, job_id)
        if cmd == "catalog": return await self._handle_catalog(inst, job_id)
        return self._fail("Invalid command", "Use extract, query, status, or catalog.")

    async def _handle_extract(self, inst, job_id: str) -> str:
        from database.connection import DatabaseManager
        from .summary import build_extract_summary
        from .models import SFFactRecord
        from .query import query_concepts

        ticker = inst.ticker
        quarters = inst.quarters
        refresh = inst.refresh

        conn = DatabaseManager.get_read_connection()
        existing = conn.execute("SELECT COUNT(DISTINCT quarter) FROM sf_quarterly_facts WHERE ticker=?", (ticker,)).fetchone()[0]
        cache_hit = existing >= quarters and not refresh
        
        if not cache_hit:
            try:
                await to_thread_with_context(extract_and_persist, ticker, quarters, refresh, job_id)
            except Exception as e:
                log.dual_log(tag="StockFin:Extract:Error", message=f"Extraction failed: {e}", level="ERROR", payload={"error": str(e)})
                return self._fail(f"Extraction failed: {e}", "Verify ticker symbol and EDGAR connectivity.")

        rows = self._fetch_rows(ticker)
        if not rows:
            return self._success(f"No data extracted for **{ticker}**.", {"ticker": ticker, "rows_extracted": 0})

        company_name = self._fetch_company_name(ticker) or ticker
        available_concepts = {stype: query_concepts(ticker, stype) for stype in ["income", "balance", "cashflow"]}

        import pandas as pd
        df = pd.DataFrame(rows)
        csv_path = write_artifact(self.name, job_id, f"{ticker}_financials", "csv", df.to_csv(index=False))

        summary = build_extract_summary(
            ticker=ticker, company_name=company_name, quarters_requested=quarters,
            quarters_cached=len({r["quarter"] for r in rows}), cache_hit=cache_hit,
            refresh=refresh, all_rows=[SFFactRecord.model_validate(r) for r in rows],
            available_concepts=available_concepts
        )
        return self._success(summary.to_markdown(), {"ticker": ticker, "rows_extracted": len(rows)}, [{"filename": csv_path.name, "type": "file", "description": "Tabular CSV Export (audit receipt)"}])

    async def _handle_query(self, inst, job_id: str) -> str:
        from .summary import build_query_summary
        from .models import SFFactRecord
        records = await to_thread_with_context(query_facts, inst.ticker, inst.statement_type, inst.concept, inst.start_quarter, inst.end_quarter, inst.limit)
        if not records:
            return self._fail(f"No records found for **{inst.ticker}** `{inst.statement_type}`.", "Run `extract` first or use `catalog` to check concept spelling.")
        typed = [SFFactRecord.model_validate(r) for r in records]
        summary = build_query_summary(ticker=inst.ticker, statement_type=inst.statement_type, concept_filter=inst.concept, rows=typed)
        md = summary.to_markdown()
        art_path = write_artifact(self.name, job_id, f"query_{inst.ticker}_{inst.statement_type}", "md", md)
        return self._success(md, {"rows": len(records), "ticker": inst.ticker}, [{"filename": art_path.name, "type": "file", "description": "Markdown Results Table (audit receipt)"}])

    async def _handle_status(self, inst, job_id: str) -> str:
        from database.connection import DatabaseManager
        from .summary import build_status_summary
        from .query import query_concepts
        conn = DatabaseManager.get_read_connection()
        rows = conn.execute("SELECT statement_type, COUNT(DISTINCT quarter) as q_count, COUNT(*) as r_count, MAX(quarter) as latest FROM sf_quarterly_facts WHERE ticker=? GROUP BY statement_type", (inst.ticker,)).fetchall()
        per_statement = {r["statement_type"]: {"rows": r["r_count"], "quarters": r["q_count"], "latest": r["latest"]} for r in rows}
        available_concepts = {stype: query_concepts(inst.ticker, stype) for stype in per_statement.keys()}
        summary = build_status_summary(inst.ticker, per_statement, available_concepts)
        return self._success(summary.to_markdown(), {"ticker": inst.ticker, "status": per_statement})

    async def _handle_catalog(self, inst, job_id: str) -> str:
        from .summary import CatalogSummary
        from .query import query_concepts
        concepts = await to_thread_with_context(query_concepts, inst.ticker, inst.statement_type)
        if not concepts:
            return self._fail(f"No concepts found for **{inst.ticker}**.", "Run `extract` first.")
        summary = CatalogSummary(ticker=inst.ticker, statement_type=inst.statement_type, concepts=concepts)
        return self._success(summary.to_markdown(), {"ticker": inst.ticker, "concept_count": len(concepts)})

    def _fetch_rows(self, ticker: str) -> List[dict]:
        from database.connection import DatabaseManager
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM sf_quarterly_facts WHERE ticker=? ORDER BY statement_type, concept_order, quarter DESC", (ticker,)).fetchall()]

    def _fetch_company_name(self, ticker: str) -> str | None:
        from database.connection import DatabaseManager
        row = DatabaseManager.get_read_connection().execute("SELECT company_name FROM sf_tickers WHERE ticker=?", (ticker,)).fetchone()
        return row[0] if row else None

    def _fail(self, summary: str, next_steps: str) -> str:
        return json.dumps({"_callback_format": "structured", "tool_name": self.name, "status": "FAILED", "summary": summary, "status_overrides": {"FAILED": {"description": "Stock Financials failed", "next_steps": next_steps, "rerunnable": True}}}, ensure_ascii=False)

    def _success(self, summary: str, details: dict, artifacts: list = None) -> str:
        return json.dumps({"_callback_format": "structured", "tool_name": self.name, "status": "COMPLETED", "summary": summary, "details": details, "artifacts": artifacts or []}, ensure_ascii=False)
