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
SUMMARY_CHAR_BUDGET = 18_000


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

    @activity("Build Status Markdown")
    def _build_status_markdown(self, ticker: str, per_statement: dict) -> str:
        """Build the status markdown summary."""
        if not per_statement:
            return f"No cached data for **{ticker}**. Run `extract` first."
        available_concepts = {stype: query_concepts(ticker, stype) for stype in per_statement.keys()}
        lines = [f"#### Cache Status for **{ticker}**", "", "| Statement | Rows | Quarters | Latest |", "|---|---|---|---|"]
        for stype, info in per_statement.items():
            lines.append(f"| {STATEMENT_TYPES.get(stype, stype)} | {info.get('rows', 0)} | {info.get('quarters', 0)} | `{info.get('latest', '—')}` |")
        return "\n".join(lines)

    @activity("Build Extract Markdown")
    def _build_extract_markdown_activity(self, ticker: str, company_name: str, quarters_requested: int, quarters_cached: int, cache_hit: bool, refresh: bool, rows: list, available_concepts: dict) -> str:
        """Build the extract markdown summary."""
        return self._build_extract_markdown(ticker, company_name, quarters_requested, quarters_cached, cache_hit, refresh, rows, available_concepts)

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

        # Step 5: Build extract markdown.
        md = self._build_extract_markdown_activity(
            ticker, company_name, quarters,
            len({r["quarter"] for r in rows}),
            cache_hit, refresh, rows, available_concepts
        )

        await telemetry(self.status("Extraction complete", "COMPLETED"))
        return md

    def _build_extract_markdown(self, ticker: str, company_name: str, quarters_requested: int, quarters_cached: int, cache_hit: bool, refresh: bool, rows: list, available_concepts: dict) -> str:
        action = "cache hit:" if cache_hit else ("Refreshed" if refresh else "Extracted")
        lines = [f"**{ticker}** ({company_name}) — {action} {quarters_cached} quarters.\n"]

        import pandas as pd
        df = pd.DataFrame(rows)
        if "statement_type" in df.columns:
            lines.extend(["", "#### Coverage", "| Statement | Rows | Quarters | Latest |", "|---|---|---|---|"])
            for stype in ["income", "balance", "cashflow"]:
                sdf = df[df["statement_type"] == stype]
                if sdf.empty: continue
                lines.append(f"| {STATEMENT_TYPES.get(stype, stype)} | {len(sdf)} | {int(sdf['quarter'].nunique())} | `{str(sdf['quarter'].max()) if not sdf.empty else '—'}` |")

        for stype in ["income", "balance", "cashflow"]:
            sdf = df[df["statement_type"] == stype] if "statement_type" in df.columns else df.iloc[0:0]
            if sdf.empty: continue
            metrics = KEY_CONCEPTS.get(stype, {})
            if not metrics: continue
            lines.extend(["", f"#### Key {STATEMENT_TYPES.get(stype, stype)} Metrics"])
            all_qs = sorted({q for q in sdf["quarter"]} if "quarter" in sdf.columns else [], reverse=True)[:SUMMARY_QUARTERS_SHOWN]
            if not all_qs: continue
            lines.extend(["| Metric | " + " | ".join(f"`{q}`" for q in all_qs) + " |", "|---|" + "|".join(["---"] * len(all_qs)) + "|"])
            for concept_full, label in metrics.items():
                c_data = sdf[sdf["concept"] == concept_full] if "concept" in sdf.columns else sdf.iloc[0:0]
                if c_data.empty: continue
                unit_val = str(c_data["unit"].iloc[0]) if "unit" in c_data.columns and not c_data["unit"].isna().all() else "USD"
                row = [label]
                for q in all_qs:
                    qd = dict(zip(c_data["quarter"], c_data["numeric_value"])) if "quarter" in c_data.columns else {}
                    row.append(format_value(qd.get(q), unit_val))
                lines.append("| " + " | ".join(row) + " |")

        md = "\n".join(lines)
        return md[:SUMMARY_CHAR_BUDGET - 100] + "\n\n*[Truncated]*" if len(md) > SUMMARY_CHAR_BUDGET else md

    async def _handle_query(self, inst, job_id: str, telemetry: Any) -> str:
        from .models import SFFactRecord
        await telemetry(self.status(f"Querying {inst.ticker} {inst.statement_type}..."))
        records = await to_thread_with_context(query_facts, inst.ticker, inst.statement_type, inst.concept, inst.start_quarter, inst.end_quarter, inst.limit)
        if not records:
            return f"No records found for **{inst.ticker}** `{inst.statement_type}`.\n\nRun `extract` first or use `catalog` to check concept spelling."
        typed = [SFFactRecord.model_validate(r) for r in records]
        md = self._build_query_markdown(inst.ticker, inst.statement_type, inst.concept, typed)
        art_path = write_artifact(self.name, job_id, f"query_{inst.ticker}_{inst.statement_type}", "md", md)
        return md

    def _build_query_markdown(self, ticker: str, statement_type: str, concept_filter: str | None, rows: list) -> str:
        if not rows:
            return f"No records found for **{ticker}** `{statement_type}`."
        lines = [f"Found **{len(rows)}** fact(s) for **{ticker}** ({STATEMENT_TYPES.get(statement_type, statement_type)})."]
        concepts = list(dict.fromkeys(r.concept for r in rows))
        quarters = sorted({r.quarter for r in rows}, reverse=True)[:8]
        lines.extend(["", "| Concept | Label | " + " | ".join(f"`{q}`" for q in quarters) + " |", "|---|---|" + "|".join(["---"] * len(quarters)) + "|"])
        idx = {(r.concept, r.quarter): r for r in rows}
        for c in concepts:
            sample = next((r for r in rows if r.concept == c), None)
            if not sample: continue
            row = [f"`{c}`", sample.label]
            for q in quarters:
                r = idx.get((c, q))
                row.append(format_value(r.numeric_value if r else None, r.unit if r else "USD"))
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

    async def _handle_status(self, inst, job_id: str, telemetry: Any) -> str:
        from database.connection import DatabaseManager
        from .query import query_concepts
        await telemetry(self.status(f"Checking cache status for {inst.ticker}..."))

        # Step 2: Query cache status (activity-decorated).
        per_statement = self._query_cache_status(inst.ticker)

        # Step 3: Build status markdown (activity-decorated).
        md = self._build_status_markdown(inst.ticker, per_statement)
        return md

    async def _handle_catalog(self, inst, job_id: str, telemetry: Any) -> str:
        from .query import query_concepts
        await telemetry(self.status(f"Building concept catalog for {inst.ticker}..."))
        concepts = await to_thread_with_context(query_concepts, inst.ticker, inst.statement_type)
        if not concepts:
            return f"No concepts found for **{inst.ticker}**.\n\nRun `extract` first."
        st_label = f" for {STATEMENT_TYPES.get(inst.statement_type, inst.statement_type)}" if inst.statement_type else ""
        lines = [f"#### Concept Catalog for **{inst.ticker}**{st_label}", f"Found {len(concepts)} available concepts:", "", "`" + "`, `".join(concepts) + "`"]
        md = "\n".join(lines)
        return md[:SUMMARY_CHAR_BUDGET - 100] + "\n\n*[Truncated]*" if len(md) > SUMMARY_CHAR_BUDGET else md

    def _fetch_rows(self, ticker: str) -> List[dict]:
        from database.connection import DatabaseManager
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM sf_quarterly_facts WHERE ticker=? ORDER BY statement_type, concept_order, quarter DESC", (ticker,)).fetchall()]

    def _fetch_company_name(self, ticker: str) -> str | None:
        from database.connection import DatabaseManager
        row = DatabaseManager.get_read_connection().execute("SELECT company_name FROM sf_tickers WHERE ticker=?", (ticker,)).fetchone()
        return row[0] if row else None
