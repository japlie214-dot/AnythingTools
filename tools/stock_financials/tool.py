# tools/stock_financials/tool.py
"""Stock Financials Tool — SEC EDGAR quarterly fact extraction.

Returns JSON strings for sync API consumption.

Activity-Driven Observability:
  The extract, query, and status command paths are decomposed into named activities.
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
        # Propagate the artifact path to ToolResult.attachment_paths via _last_artifacts.
        # BaseTool.execute() reads self._last_artifacts at tools/base.py:242 and passes
        # it as attachment_paths in the ToolResult. Without this, the CSV path is only
        # in the JSON payload but not attached as a file to the sync API response.
        # Pattern follows stock_notes/tool.py:206.
        self._last_artifacts = [str(csv_path)]

        # Step 5: Build extract payload.
        payload = self._build_extract_payload(
            ticker, company_name, quarters,
            len({r["quarter"] for r in rows}),
            cache_hit, refresh, rows, available_concepts
        )
        # Note: CSV artifact is already written via write_artifact at line 255.
        # We can include the path in the payload for convenience.
        payload["csv_path"] = csv_path

        await telemetry(self.status("Extraction complete", "COMPLETED"))
        return json.dumps(payload, ensure_ascii=False, default=str)


    # --- Query command activities ---

    @activity("Fetch Query Facts")
    async def _fetch_query_facts(
        self, ticker: str, statement_type: str, concept: str | None,
        start_quarter: str | None, end_quarter: str | None, limit: int,
    ) -> list[dict]:
        """Fetch and validate facts from the operational DB.

        Returns a list of validated fact dicts (SFFactRecord.model_dump()).
        Raises ToolExecutionError on validation failure.

        NOTE on Lineage masking: If the returned list serializes to >50,000 chars,
        _cap_top_level_value (utils/observability/masking.py:213-220) replaces the
        entire list output in the Lineage with [MASKED: list-cap-exceeded - N chars, M items].
        This is expected and acceptable — the full data is preserved in the JSON artifact
        written by _write_facts_artifact. The Lineage is a trace, not a data store
        (Developer Contract utils/observability/__init__.py:46-49).
        """
        from .models import SFFactRecord
        records = await to_thread_with_context(
            query_facts, ticker, statement_type, concept,
            start_quarter, end_quarter, limit,
        )
        # Validate each record against the Pydantic model. Raises ValidationError
        # if a record has an unexpected shape — the @activity decorator records
        # FAILED and re-raises (never swallows, per §4.3.b).
        typed = [SFFactRecord.model_validate(r) for r in records]
        return [r.model_dump() for r in typed]

    @activity("Write Facts Artifact")
    def _write_facts_artifact(
        self, ticker: str, statement_type: str, facts: list[dict], job_id: str,
    ) -> str | None:
        """Offload the facts list to a JSON artifact file.

        Returns the absolute path string on success, None on failure (graceful
        degradation — the query still returns a payload with fact_count; only
        the artifact_path is None). Pattern follows stock_notes/tool.py:203-209.

        JSON (not markdown) per the convention: structured records belong in JSON,
        not prose. Ref: Pushback 4 in the plan review.
        """
        artifact_content = json.dumps(facts, ensure_ascii=False, default=str)
        artifact_type = f"{ticker}_{statement_type}_facts"
        try:
            art_path = write_artifact(self.name, job_id, artifact_type, "json", artifact_content)
            path_str = str(art_path)
            # Propagate to ToolResult.attachment_paths so the worker attaches
            # the JSON file to the sync API response. Ref: tools/base.py:242.
            self._last_artifacts = [path_str]
            return path_str
        except Exception as e:
            # Graceful degradation: log WARNING, return None. The payload still
            # carries fact_count so the LLM knows data exists; only the file
            # attachment is missing. Ref: stock_notes/tool.py:207-209.
            log.dual_log(
                tag="StockFin:QueryArtifact:Failed",
                message=f"Failed to write facts artifact: {e}",
                level="WARNING",
                payload={"error": str(e), "ticker": ticker, "statement_type": statement_type},
            )
            return None

    @activity("Build Query Payload")
    def _build_query_payload(
        self, ticker: str, statement_type: str, concept: str | None,
        facts: list[dict], artifact_path: str | None,
    ) -> dict:
        """Build the JSON summary payload for the query command.

        Returns a metadata dict — NOT the raw facts. The full facts are in the
        JSON artifact (artifact_path). This follows the Developer Contract:
        "For payloads larger than 50,000 chars per top-level key, write an
        artifact via write_artifact(...) and return the path"
        (utils/observability/__init__.py:46-49).
        """
        # Extract lightweight metadata for the LLM: distinct quarters and concepts
        # give the agent enough context to decide whether to read the artifact,
        # without bloating the payload. These are derived from the facts list
        # and are small (quarter strings are ~7 chars, concept strings ~30 chars).
        quarters = sorted({f.get("quarter") for f in facts if f.get("quarter")})
        concepts = sorted({f.get("concept") for f in facts if f.get("concept")})
        return {
            "ticker": ticker,
            "statement_type": statement_type,
            "concept_filter": concept,
            "fact_count": len(facts),
            "artifact_path": artifact_path,
            "quarters": quarters,
            "concepts": concepts,
        }

    async def _handle_query(self, inst, job_id: str, telemetry: Any) -> str:
        """Entry-point orchestrator for the query command.

        Decomposes into three named Activities (per §4.3 of the Developer Contract):
          1. Fetch Query Facts — read + validate from DB
          2. Write Facts Artifact — offload to JSON file
          3. Build Query Payload — assemble the metadata summary

        The Accumulator is created and bound by bot/engine/worker.py::_run_job
        when capture_lineage=true (Developer Contract utils/observability/__init__.py:51-53).
        The @activity decorator reads it from contextvars — no explicit threading
        needed in tool code (§4.3.c).
        """
        await telemetry(self.status(f"Querying {inst.ticker} {inst.statement_type}..."))

        facts = await self._fetch_query_facts(
            inst.ticker, inst.statement_type, inst.concept,
            inst.start_quarter, inst.end_quarter, inst.limit,
        )
        if not facts:
            return json.dumps(
                {"error": f"No records found for {inst.ticker} {inst.statement_type}."},
                ensure_ascii=False, default=str,
            )

        artifact_path = self._write_facts_artifact(
            inst.ticker, inst.statement_type, facts, job_id,
        )
        payload = self._build_query_payload(
            inst.ticker, inst.statement_type, inst.concept,
            facts, artifact_path,
        )
        return json.dumps(payload, ensure_ascii=False, default=str)


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
