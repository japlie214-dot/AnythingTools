# tools/stock_financials/summary.py
"""
Presentation layer for stock_financials tool.

Owns all human-readable constants (statement-type labels, per-share unit
sets, key-concept map, summary size budgets) and the markdown renderers
for extract / query / status / catalog commands.

Concept format contract
-----------------------
All concepts are stored and displayed in their raw SEC EDGAR XBRL form
(e.g. ``us-gaap:Assets``), per the FASB US-GAAP taxonomy convention.
See:
  - https://xbrl.org/guidance/xbrl-glossary  (namespace prefix)
  - https://www.sec.gov/data-research/structured-data/inline-xbrl/xbrl-glossary-terms

No ``:`` → ``_`` normalization is performed anywhere in the pipeline.
The ``stock_notes`` tool already follows this convention; this module
and the extractor now match it.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from tools.stock_financials.models import SFFactRecord

# ─── Presentation-layer constants (formerly in constants.py) ──────────────────

STATEMENT_TYPES: Dict[str, str] = {
    "income": "Income Statement",
    "balance": "Balance Sheet",
    "cashflow": "Cash Flow Statement",
}

# Per https://docs.sec.gov/edgar/xbrl-identification - the SEC accepts a
# free-form unit string on XBRL facts. We classify into three buckets:
#   - per-share (EPS, dividends per share) → 2-4 decimal places
#   - share counts (shares outstanding, weighted-average shares) → M/B suffix
#   - monetary (everything else) → currency-code-aware formatting
PER_SHARE_UNITS: frozenset = frozenset({"USD per share", "USD/shares", "TWD per share", "JPY per share", "EUR per share", "GBP per share"})
SHARE_UNITS: frozenset = frozenset({"shares"})

# ISO 4217 currency code → display symbol. Falls back to the raw code
# (e.g. "KRW") for unmapped currencies, which is safer than hallucinating $.
# Reference: https://www.iso.org/iso-4217-currency-codes.html
_CURRENCY_SYMBOLS: Dict[str, str] = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "TWD": "NT$",
    "CNY": "¥",
    "KRW": "₩",
    "INR": "₹",
    "AUD": "A$",
    "CAD": "C$",
    "CHF": "CHF",
    "SGD": "S$",
    "HKD": "HK$",
}

# Key concepts surfaced in the extract summary's "Key Metrics" table.
# Concepts use the canonical SEC EDGAR XBRL format (us-gaap:Foo).
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


def _extract_currency_code(unit: str) -> Optional[str]:
    """Extract an ISO 4217 currency code from an XBRL unit string.

    XBRL units come in shapes like ``USD``, ``USD per share``, ``USD/shares``,
    ``TWD``, ``JPY per share``, ``shares``, ``pure``. We split on whitespace
    and slash, then take the first token. ``shares`` and ``pure`` return None
    (they're not currency codes).
    """
    if not unit:
        return None
    # Handle "USD per share", "USD/shares" → "USD"
    # Handle "shares", "pure" → None
    first_token = unit.strip().split()[0].split("/")[0].upper()
    if first_token in {"SHARES", "PURE", ""}:
        return None
    return first_token


def _currency_symbol(currency_code: Optional[str]) -> str:
    """Map ISO currency code to display symbol. Falls back to the raw code."""
    if not currency_code:
        return "$"  # Historical default for backward compatibility
    return _CURRENCY_SYMBOLS.get(currency_code, currency_code)


def format_value(value: Optional[float], unit: str = "USD") -> str:
    """Format a numeric fact for human display.

    Behavior matrix:
      - None / NaN                              → "—"
      - unit in PER_SHARE_UNITS                 → "<symbol>1.23" (2-4 decimals)
      - unit in SHARE_UNITS                     → "1.23B" / "1.23M" / "1,234"
      - monetary (else)                         → "<symbol>1.2B" / "<symbol>1.2M" / "<symbol>1,234"

    The currency symbol is derived from the ``unit`` field via
    ``_extract_currency_code``. For non-USD currencies the ISO code is
    preserved (e.g. ``NT$`` for TWD, ``¥`` for JPY) so the reader never
    confuses a TWD figure for a USD one. See W5 in the refactor plan.
    """
    if value is None:
        return "—"
    try:
        val = float(value)
    except (TypeError, ValueError):
        return str(value)

    currency_code = _extract_currency_code(unit)
    symbol = _currency_symbol(currency_code)

    if unit in PER_SHARE_UNITS:
        # Per-share values: 2 decimals for normal magnitudes, 4 for sub-cent
        return f"{symbol}{val:,.2f}" if abs(val) >= 0.01 else f"{symbol}{val:.4f}"
    if unit in SHARE_UNITS:
        if abs(val) >= 1_000_000_000:
            return f"{val / 1_000_000_000:,.2f}B"
        if abs(val) >= 1_000_000:
            return f"{val / 1_000_000:,.2f}M"
        return f"{val:,.0f}"
    # Monetary — apply currency-symbol prefix and magnitude suffix
    if abs(val) >= 1_000_000_000:
        return f"{symbol}{val / 1_000_000_000:,.1f}B"
    if abs(val) >= 1_000_000:
        return f"{symbol}{val / 1_000_000:,.1f}M"
    return f"{symbol}{val:,.0f}"


def _append_concepts_list(lines: List[str], available_concepts: Dict[str, List[str]]):
    if not available_concepts:
        return
    lines.append("\n#### Available Concepts (Subset)")
    for stype, concepts in available_concepts.items():
        displayed = concepts[:30]
        lines.append(f"**{STATEMENT_TYPES.get(stype, stype)}**:")
        lines.append(f"`{'`, `'.join(displayed)}`")
        if len(concepts) > 30:
            lines.append(f"*(...and {len(concepts) - 30} more. Use the `catalog` command to see all.)*")
    lines.append("")


@dataclass
class ExtractSummary:
    ticker: str
    company_name: str
    quarters_requested: int
    quarters_cached: int
    cache_hit: bool
    refresh: bool
    per_statement: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # key_metrics[statement_type][label][quarter] = numeric_value
    # Plus a parallel 'unit' map for currency-aware formatting.
    key_metrics: Dict[str, Dict[str, Dict[str, float]]] = field(default_factory=dict)
    key_metric_units: Dict[str, Dict[str, str]] = field(default_factory=dict)
    available_concepts: Dict[str, List[str]] = field(default_factory=dict)

    def to_markdown(self) -> str:
        lines = []
        action = "cache hit:" if self.cache_hit else ("Refreshed" if self.refresh else "Extracted")
        lines.append(f"**{self.ticker}** ({self.company_name}) — {action} {self.quarters_cached} quarters.")

        if self.per_statement:
            lines.extend(["", "#### Coverage", "| Statement | Rows | Quarters | Latest |", "|---|---|---|---|"])
            for stype, info in self.per_statement.items():
                lines.append(f"| {STATEMENT_TYPES.get(stype, stype)} | {info.get('rows', 0)} | {info.get('quarters', 0)} | `{info.get('latest', '—')}` |")

        # Lookup table for the unit associated with each (statement_type, label)
        # so format_value can render with the correct currency symbol.
        unit_lookup = self.key_metric_units

        for stype, metrics in self.key_metrics.items():
            if not metrics:
                continue
            lines.extend(["", f"#### Key {STATEMENT_TYPES.get(stype, stype)} Metrics"])
            all_qs = sorted({q for qd in metrics.values() for q in qd.keys()}, reverse=True)[:SUMMARY_QUARTERS_SHOWN]
            if not all_qs:
                continue
            lines.extend(["| Metric | " + " | ".join(f"`{q}`" for q in all_qs) + " |", "|---|" + "|".join(["---"] * len(all_qs)) + "|"])
            for label, qd in metrics.items():
                # Pull the actual unit from the parallel map; default to "USD"
                # for backward compat with any rows missing the field.
                unit = unit_lookup.get(stype, {}).get(label, "USD")
                row = [f"{label}"]
                for q in all_qs:
                    row.append(format_value(qd.get(q), unit))
                lines.append("| " + " | ".join(row) + " |")

        _append_concepts_list(lines, self.available_concepts)

        md = "\n".join(lines)
        return md[:SUMMARY_CHAR_BUDGET - 100] + "\n\n*[Truncated]*" if len(md) > SUMMARY_CHAR_BUDGET else md


@dataclass
class QuerySummary:
    ticker: str
    statement_type: str
    concept_filter: Optional[str]
    rows: List[SFFactRecord]

    def to_markdown(self) -> str:
        if not self.rows:
            return f"No records found for **{self.ticker}** `{self.statement_type}`."
        lines = [f"Found **{len(self.rows)}** fact(s) for **{self.ticker}** ({STATEMENT_TYPES.get(self.statement_type, self.statement_type)})."]
        concepts = []
        seen = set()
        for r in self.rows:
            if r.concept not in seen:
                concepts.append(r.concept)
                seen.add(r.concept)
        quarters = sorted({r.quarter for r in self.rows}, reverse=True)[:8]
        lines.extend(["", "| Concept | Label | " + " | ".join(f"`{q}`" for q in quarters) + " |", "|---|---|" + "|".join(["---"] * len(quarters)) + "|"])
        idx = {(r.concept, r.quarter): r for r in self.rows}
        for c in concepts:
            sample = next((r for r in self.rows if r.concept == c), None)
            if not sample:
                continue
            row = [f"`{c}`", sample.label]
            for q in quarters:
                r = idx.get((c, q))
                # Read the unit directly from the fact record — no heuristic.
                row.append(format_value(r.numeric_value if r else None, r.unit if r else "USD"))
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)


@dataclass
class StatusSummary:
    ticker: str
    per_statement: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    available_concepts: Dict[str, List[str]] = field(default_factory=dict)

    def to_markdown(self) -> str:
        if not self.per_statement:
            return f"No cached data for **{self.ticker}**. Run `extract` first."
        lines = [f"#### Cache Status for **{self.ticker}**", "", "| Statement | Rows | Quarters | Latest |", "|---|---|---|---|"]
        for stype, info in self.per_statement.items():
            lines.append(f"| {STATEMENT_TYPES.get(stype, stype)} | {info.get('rows', 0)} | {info.get('quarters', 0)} | `{info.get('latest', '—')}` |")
        _append_concepts_list(lines, self.available_concepts)
        return "\n".join(lines)


@dataclass
class CatalogSummary:
    ticker: str
    statement_type: Optional[str]
    concepts: List[str]

    def to_markdown(self) -> str:
        st_label = f" for {STATEMENT_TYPES.get(self.statement_type, self.statement_type)}" if self.statement_type else ""
        lines = [f"#### Concept Catalog for **{self.ticker}**{st_label}", f"Found {len(self.concepts)} available concepts:", ""]
        lines.append("`" + "`, `".join(self.concepts) + "`")
        md = "\n".join(lines)
        return md[:SUMMARY_CHAR_BUDGET - 100] + "\n\n*[Truncated]*" if len(md) > SUMMARY_CHAR_BUDGET else md


def build_extract_summary(
    ticker: str,
    company_name: str,
    quarters_requested: int,
    quarters_cached: int,
    cache_hit: bool,
    refresh: bool,
    all_rows: List[SFFactRecord],
    available_concepts: Dict[str, List[str]],
) -> ExtractSummary:
    """Build an ExtractSummary from a list of SFFactRecord Pydantic models.

    Per the Pydantic v2 docs, calling ``pd.DataFrame(list_of_models)``
    does NOT extract model fields into columns (models are iterable but
    not Mappings). The canonical pattern is
    ``pd.DataFrame([m.model_dump() for m in models])``:
      https://docs.pydantic.dev/latest/api/base_model/#pydantic.BaseModel.model_dump
    """
    import pandas as pd

    per_statement: Dict[str, Dict[str, int]] = {}
    key_metrics: Dict[str, Dict[str, Dict[str, float]]] = {}
    key_metric_units: Dict[str, Dict[str, str]] = {}

    # Pre-flight: empty rows shouldn't blow up the DataFrame constructor.
    if not all_rows:
        return ExtractSummary(
            ticker=ticker, company_name=company_name,
            quarters_requested=quarters_requested, quarters_cached=quarters_cached,
            cache_hit=cache_hit, refresh=refresh,
            per_statement={}, key_metrics={}, key_metric_units={},
            available_concepts=available_concepts,
        )

    # Materialize dicts BEFORE handing to pandas — model_dump() is the
    # Pydantic-blessed way (see doc link above).
    rows_as_dicts = [r.model_dump() for r in all_rows]
    df = pd.DataFrame(rows_as_dicts)

    for stype in ["income", "balance", "cashflow"]:
        if "statement_type" not in df.columns:
            # Defensive: shouldn't happen given SFFactRecord.statement_type
            # is required, but guard against future schema drift.
            break
        sdf = df[df["statement_type"] == stype]
        if sdf.empty:
            continue
        per_statement[stype] = {
            "rows": len(sdf),
            "quarters": int(sdf["quarter"].nunique()),
            "latest": str(sdf["quarter"].max()) if not sdf.empty else "—",
        }

        # Surface key concepts with their actual unit (no heuristic).
        metrics = KEY_CONCEPTS.get(stype, {})
        st_metrics: Dict[str, Dict[str, float]] = {}
        st_units: Dict[str, str] = {}
        for concept_full, label in metrics.items():
            # Exact match — concepts are now stored in raw us-gaap:Foo form.
            c_data = sdf[sdf["concept"] == concept_full]
            if not c_data.empty:
                # Take the unit from the first matching row.
                unit_val = str(c_data["unit"].iloc[0]) if "unit" in c_data.columns and not c_data["unit"].isna().all() else "USD"
                st_units[label] = unit_val
                # Map quarter → numeric_value
                st_metrics[label] = dict(zip(c_data["quarter"], c_data["numeric_value"]))
        if st_metrics:
            key_metrics[stype] = st_metrics
            key_metric_units[stype] = st_units

    return ExtractSummary(
        ticker=ticker, company_name=company_name,
        quarters_requested=quarters_requested, quarters_cached=quarters_cached,
        cache_hit=cache_hit, refresh=refresh,
        per_statement=per_statement,
        key_metrics=key_metrics,
        key_metric_units=key_metric_units,
        available_concepts=available_concepts,
    )


def build_query_summary(ticker: str, statement_type: str, concept_filter: Optional[str], rows: List[SFFactRecord]) -> QuerySummary:
    return QuerySummary(ticker=ticker, statement_type=statement_type, concept_filter=concept_filter, rows=rows)


def build_status_summary(ticker: str, per_statement: Dict[str, Dict[str, Any]], available_concepts: Dict[str, List[str]]) -> StatusSummary:
    return StatusSummary(ticker=ticker, per_statement=per_statement, available_concepts=available_concepts)
