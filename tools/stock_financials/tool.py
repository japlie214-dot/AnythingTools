# tools/stock_financials/tool.py
"""Stock Financials Tool — SEC EDGAR quarterly fact extraction.

Returns plain markdown strings for sync API consumption.

Activity-Driven Observability:
  The extract and status command paths are decomposed into named activities.
  See utils/observability/activity_decorator.py.
"""

import json
import sqlite3
from typing import Any, List, Dict
from tools.base import BaseTool, ToolExecutionError, ToolValidationError
from utils.logger import get_dual_logger
from utils.context_helpers import to_thread_with_context
from utils.artifact_manager import write_artifact
from utils.observability.activity_decorator import activity
from .models import StockFinancialsInput, SFFactRecord
from .extractor import extract_and_persist
from .query import query_facts, query_concepts

log = get_dual_logger(__name__)

# ─── Presentation constants ──────────────────────────────────────────────

STATEMENT_TYPES: Dict[str, str] = {
    "income": "Income Statement",
    "balance": "Balance Sheet",
    "cashflow": "Cash Flow Statement",
}

PER_SHARE_UNITS = frozenset({"USD per share", "USD/shares", "TWD per share", "JPY per share", "EUR per share", "GBP per share"})
SHARE_UNITS = frozenset({"shares"})

_CURRENCY_SYMBOLS: Dict[str, str] = {
    "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "TWD": "NT$",
    "CNY": "¥", "KRW": "₩", "INR": "₹", "AUD": "A$", "CAD": "C$",
    "CHF": "CHF", "SGD": "S$", "HKD": "HK$",
}

KEY_CONCEPTS: Dict[str, Dict[str, str]] = {
    "income": {
        "us-gaap:Revenues": "Revenue",
        "us-gaap:GrossProfit": "Gross Profit",
        "us-gaap:OperatingIncomeLoss": "Operating Income",
        "us-gaap:NetIncomeLoss": "Net Income",
        "us-gaap:EarningsPerShareBasic": "EPS (Basic)",
    },
    "balance": {
        "us-gaap:Assets": "Total Assets",
        "us-gaap:Liabilities": "Total Liabilities",
        "us-gaap:StockholdersEquity": "Stockholders' Equity",
        "us-gaap:CashAndCashEquivalentsAtCarryingValue": "Cash & Equivalents",
    },
    "cashflow": {
        "us-gaap:NetCashProvidedByUsedInOperatingActivities": "Operating CF",
        "us-gaap:NetCashProvidedByUsedInInvestingActivities": "Investing CF",
    },
}

SUMMARY_QUARTERS_SHOWN = 4


def _extract_currency_code(unit: str) -> str | None:
    if not unit:
        return None
    first_token = unit.strip().split()[0].split("/")[0].upper()
    if first_token in {"SHARES", "PURE", ""}:
        return None
    return first_token


def _currency_symbol(currency_code: str | None) -> str:
    if not currency_code:
        return "$"
    return _CURRENCY_SYMBOLS.get(currency_code, currency_code)


def format_value(value: float | None, unit: str = "USD") -> str:
    if value is None:
        return "—"
    try:
        val = float(value)
    except (TypeError, ValueError):
        return str(value)
    currency_code = _extract_currency_code(unit)
    symbol = _currency_symbol(currency_code)
    if unit in PER_SHARE_UNITS:
        return f"{symbol}{val:,.2f}" if abs(val) >= 0.01 else f"{symbol}{val:.4f}"
    if unit in SHARE_UNITS:
        if abs(val) >= 1_000_000_000: return f"{val / 1_000_000_000:,.2f}B"
        if abs(val) >= 1_000_000: return f"{val / 1_000_000:,.2f}M"
        return f"{val:,.0f}"
    if abs(val) >= 1_000_000_000: return f"{symbol}{val / 1_000_000_000:,.1f}B"
    if abs(val) >= 1_000_000: return f"{symbol}{val / 1_000_000:,.1f}M"
    return f"{symbol}{val:,.0f}"


class StockFinancialsTool(BaseTool):
    name = "stock_financials"
    INPUT_MODEL = StockFinancialsInput

    def is_resumable(self, args: dict[str, Any]) -> bool:
        return True

    # --- Activity-decomposed sub-methods ---

    @activity("Validate StockFinancialsInput")
    def _validate_input(self, args: dict, job_id: str):
        """Validate input args against the Pydantic model. Raises on invalid."""
        try:
            validated = StockFinancialsInput.model_validate(args)
            inst = validated.resolved_instructions()
        except Exception as e:
            raise ToolValidationError(
                f"Invalid input: {e}",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Check the command and instructions shape.",
            ) from e
        return validated, inst

    @activity("Check Cache Hit")
    def _check_cache_hit(self, ticker: str, quarters: int, refresh: bool) -> bool:
        """Check if the ticker's data is already cached. Returns True on cache hit."""
        from database.connection import DatabaseManager
        conn = DatabaseManager.get_read_connection()
        existing = conn.execute(
            "SELECT COUNT(DISTINCT quarter) FROM sf_quarterly_facts WHERE ticker=?", (ticker,)
        ).fetchone()[0]
        return existing >= quarters and not refresh

    @activity("Extract and Persist Facts")
    async def _extract_and_persist(self, ticker: str, quarters: int, refresh: bool, job_id: str) -> None:
        """Call EDGAR and persist facts to DB. Raises on EDGAR failure."""
        try:
            await to_thread_with_context(extract_and_persist, ticker, quarters, refresh, job_id)
        except Exception as e:
            log.dual_log(tag="StockFin:Extract:Error", message=f"Extraction failed: {e}", level="ERROR", payload={"error": str(e)})
            raise ToolExecutionError(
                f"Extraction failed: {e}",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Verify ticker symbol and EDGAR connectivity.",
            )

    @activity("Fetch Rows")
    def _fetch_rows_activity(self, ticker: str) -> list:
        """Read persisted rows from DB."""
        return self._fetch_rows(ticker)

    @activity("Query Cache Status")
    def _query_cache_status(self, ticker: str) -> dict:
        """Query the DB for cached quarter counts per statement type."""
        from database.connection import DatabaseManager
        conn = DatabaseManager.get_read_connection()
        rows = conn.execute(
            "SELECT statement_type, COUNT(DISTINCT quarter) as q_count, COUNT(*) as r_count, MAX(quarter) as latest "
            "FROM sf_quarterly_facts WHERE ticker=? GROUP BY statement_type",
            (ticker,)
        ).fetchall()
        return {r["statement_type"]: {"rows": r["r_count"], "quarters": r["q_count"], "latest": r["latest"]} for r in rows}

    @activity("Build Status Payload")
    def _build_status_payload(self, ticker: str, per_statement: dict) -> dict:
        """Build JSON payload for status command."""
        available_concepts = {stype: query_concepts(ticker, stype) for stype in per_statement.keys()}
        return {
            "ticker": ticker,
            "per_statement": per_statement,
            "available_concepts": available_concepts,
        }

    @activity("Build Extract Payload")
    def _build_extract_payload(self, ticker: str, company_name: str, quarters_requested: int, quarters_cached: int, cache_hit: bool, refresh: bool, rows: list, available_concepts: dict) -> dict:
        """Build JSON payload for extract command."""
        from collections import defaultdict
        coverage = defaultdict(lambda: {"rows": 0, "quarters": set(), "latest": None})
        for row in rows:
            stype = row.get("statement_type", "unknown")
            coverage[stype]["rows"] += 1
            q = row.get("quarter")
            if q:
                coverage[stype]["quarters"].add(q)
                if coverage[stype]["latest"] is None or q > coverage[stype]["latest"]:
                    coverage[stype]["latest"] = q

        coverage_json = {}
        for stype, info in coverage.items():
            coverage_json[stype] = {
                "rows": info["rows"],
                "quarters": len(info["quarters"]),
                "latest": info["latest"],
            }

        return {
            "ticker": ticker,
            "company_name": company_name,
            "quarters_cached": quarters_cached,
            "cache_hit": cache_hit,
            "refresh": refresh,
            "coverage": coverage_json,
            "available_concepts": available_concepts,
        }

    # --- Entry point ---

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        job_id = kwargs.get("job_id", "")

        # Step 1: Validate input (raises on invalid).
        validated, inst = self._validate_input(args, job_id)

        cmd = validated.command
        if cmd == "extract": return await self._handle_extract(inst, job_id, telemetry)
        if cmd == "query": return await self._handle_query(inst, job_id, telemetry)
        if cmd == "status": return await self._handle_status(inst, job_id, telemetry)
        if cmd == "catalog": return await self._handle_catalog(inst, job_id, telemetry)
        raise ToolExecutionError(
            "Invalid command. Use extract, query, status, or catalog.",
            tool_name=self.name,
            job_id=job_id,
        )

    async def _handle_extract(self, inst, job_id: str, telemetry: Any) -> str:
        from database.connection import DatabaseManager
        from .models import SFFactRecord
        from .query import query_concepts

        ticker = inst.ticker
        quarters = inst.quarters
        refresh = inst.refresh

        await telemetry(self.status(f"Extracting {ticker} financials ({quarters} quarters)..."))

        # Step 2: Check cache hit.
        cache_hit = self._check_cache_hit(ticker, quarters, refresh)

        # Step 3: Extract and persist (skipped on cache hit, raises on failure).
        if not cache_hit:
            await self._extract_and_persist(ticker, quarters, refresh, job_id)

        # Step 4: Fetch rows.
        rows = self._fetch_rows_activity(ticker)
        if not rows:
            return f"No data extracted for **{ticker}**."

        company_name = self._fetch_company_name(ticker) or ticker
        available_concepts = {stype: query_concepts(ticker, stype) for stype in ["income", "balance", "cashflow"]}

        # Write CSV artifact (not an activity — pure I/O, no business logic).
        import pandas as pd
        df = pd.DataFrame(rows)
        csv_path = write_artifact(self.name, job_id, f"{ticker}_financials", "csv", df.to_csv(index=False))

        # Step 5: Build extract payload.
        payload = self._build_extract_payload(
            ticker, company_name, quarters,
            len({r["quarter"] for r in rows}),
            cache_hit, refresh, rows, available_concepts
        )
        # Note: CSV artifact is already written via write_artifact at line 230.
        # We can include the path in the payload for convenience.
        payload["csv_path"] = csv_path

        await telemetry(self.status("Extraction complete", "COMPLETED"))
        return json.dumps(payload, ensure_ascii=False, default=str)


    async def _handle_query(self, inst, job_id: str, telemetry: Any) -> str:
        from .models import SFFactRecord
        await telemetry(self.status(f"Querying {inst.ticker} {inst.statement_type}..."))
        records = await to_thread_with_context(query_facts, inst.ticker, inst.statement_type, inst.concept, inst.start_quarter, inst.end_quarter, inst.limit)
        if not records:
            return json.dumps({"error": f"No records found for {inst.ticker} {inst.statement_type}."}, ensure_ascii=False, default=str)
        typed = [SFFactRecord.model_validate(r) for r in records]
        facts = [r.model_dump() for r in typed]

        return json.dumps({
            "ticker": inst.ticker,
            "statement_type": inst.statement_type,
            "concept_filter": inst.concept,
            "facts": facts,
        }, ensure_ascii=False, default=str)


    async def _handle_status(self, inst, job_id: str, telemetry: Any) -> str:
        await telemetry(self.status(f"Checking cache status for {inst.ticker}..."))
        per_statement = self._query_cache_status(inst.ticker)
        return json.dumps(self._build_status_payload(inst.ticker, per_statement), ensure_ascii=False, default=str)

    async def _handle_catalog(self, inst, job_id: str, telemetry: Any) -> str:
        from .query import query_concepts
        await telemetry(self.status(f"Building concept catalog for {inst.ticker}..."))
        concepts = await to_thread_with_context(query_concepts, inst.ticker, inst.statement_type)
        return json.dumps({
            "ticker": inst.ticker,
            "statement_type": inst.statement_type,
            "concepts": concepts,
        }, ensure_ascii=False, default=str)

    def _fetch_rows(self, ticker: str) -> List[dict]:
        from database.connection import DatabaseManager
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM sf_quarterly_facts WHERE ticker=? ORDER BY statement_type, concept_order, quarter DESC", (ticker,)).fetchall()]

    def _fetch_company_name(self, ticker: str) -> str | None:
        from database.connection import DatabaseManager
        row = DatabaseManager.get_read_connection().execute("SELECT company_name FROM sf_tickers WHERE ticker=?", (ticker,)).fetchone()
        return row[0] if row else None
