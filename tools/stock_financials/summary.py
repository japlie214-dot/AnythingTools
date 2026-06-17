# tools/stock_financials/summary.py
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from tools.stock_financials.constants import STATEMENT_TYPES, PER_SHARE_UNITS, SHARE_UNITS, KEY_CONCEPTS, SUMMARY_QUARTERS_SHOWN, SUMMARY_CHAR_BUDGET
from tools.stock_financials.models import SFFactRecord

def format_value(value: Optional[float], unit: str = "USD") -> str:
    if value is None: return "—"
    try: val = float(value)
    except (TypeError, ValueError): return str(value)
    if unit in PER_SHARE_UNITS: return f"${val:,.2f}" if abs(val) >= 0.01 else f"${val:.4f}"
    if unit in SHARE_UNITS:
        if abs(val) >= 1_000_000_000: return f"{val / 1_000_000_000:,.2f}B"
        if abs(val) >= 1_000_000: return f"{val / 1_000_000:,.2f}M"
        return f"{val:,.0f}"
    if abs(val) >= 1_000_000_000: return f"${val / 1_000_000_000:,.1f}B"
    if abs(val) >= 1_000_000: return f"${val / 1_000_000:,.1f}M"
    return f"${val:,.0f}"

def _append_concepts_list(lines: List[str], available_concepts: Dict[str, List[str]]):
    if not available_concepts: return
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
    key_metrics: Dict[str, Dict[str, Dict[str, float]]] = field(default_factory=dict)
    available_concepts: Dict[str, List[str]] = field(default_factory=dict)

    def to_markdown(self) -> str:
        lines = []
        action = "cache hit:" if self.cache_hit else ("Refreshed" if self.refresh else "Extracted")
        lines.append(f"**{self.ticker}** ({self.company_name}) — {action} {self.quarters_cached} quarters.")
        
        if self.per_statement:
            lines.extend(["", "#### Coverage", "| Statement | Rows | Quarters | Latest |", "|---|---|---|---|"])
            for stype, info in self.per_statement.items():
                lines.append(f"| {STATEMENT_TYPES.get(stype, stype)} | {info.get('rows', 0)} | {info.get('quarters', 0)} | `{info.get('latest', '—')}` |")
        
        for stype, metrics in self.key_metrics.items():
            if not metrics: continue
            lines.extend(["", f"#### Key {STATEMENT_TYPES.get(stype, stype)} Metrics"])
            all_qs = sorted({q for qd in metrics.values() for q in qd.keys()}, reverse=True)[:SUMMARY_QUARTERS_SHOWN]
            if not all_qs: continue
            lines.extend(["| Metric | " + " | ".join(f"`{q}`" for q in all_qs) + " |", "|---|" + "|".join(["---"] * len(all_qs)) + "|"])
            for concept, qd in metrics.items():
                row = [f"{concept}"]
                unit = "USD per share" if "PerShare" in concept or "Dividends" in concept else ("shares" if "Shares" in concept else "USD")
                for q in all_qs: row.append(format_value(qd.get(q), unit))
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
        if not self.rows: return f"No records found for **{self.ticker}** `{self.statement_type}`."
        lines = [f"Found **{len(self.rows)}** fact(s) for **{self.ticker}** ({STATEMENT_TYPES.get(self.statement_type, self.statement_type)})."]
        concepts = []
        seen = set()
        for r in self.rows:
            if r.concept not in seen:
                concepts.append(r.concept); seen.add(r.concept)
        quarters = sorted({r.quarter for r in self.rows}, reverse=True)[:8]
        lines.extend(["", "| Concept | Label | " + " | ".join(f"`{q}`" for q in quarters) + " |", "|---|---|" + "|".join(["---"] * len(quarters)) + "|"])
        idx = {(r.concept, r.quarter): r for r in self.rows}
        for c in concepts:
            sample = next((r for r in self.rows if r.concept == c), None)
            if not sample: continue
            row = [f"`{c}`", sample.label]
            for q in quarters:
                r = idx.get((c, q))
                row.append(format_value(r.numeric_value if r else None, r.unit if r else "USD"))
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

@dataclass
class StatusSummary:
    ticker: str
    per_statement: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    available_concepts: Dict[str, List[str]] = field(default_factory=dict)

    def to_markdown(self) -> str:
        if not self.per_statement: return f"No cached data for **{self.ticker}**. Run `extract` first."
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

def build_extract_summary(ticker, company_name, quarters_requested, quarters_cached, cache_hit, refresh, all_rows, available_concepts):
    # Helper to build ExtractSummary from raw rows
    per_statement = {}
    key_metrics = {}
    
    # Group by statement and calculate basic stats
    import pandas as pd
    df = pd.DataFrame(all_rows)
    for stype in ["income", "balance", "cashflow"]:
        sdf = df[df["statement_type"] == stype]
        if sdf.empty: continue
        per_statement[stype] = {
            "rows": len(sdf),
            "quarters": len(sdf["quarter"].unique()),
            "latest": sdf["quarter"].max() if not sdf.empty else "—"
        }
        
        # Extract key metrics defined in constants
        metrics = KEY_CONCEPTS.get(stype, {})
        st_metrics = {}
        for concept_full, label in metrics.items():
            # match if concept starts with or equals concept_full (to handle us-gaap: prefix)
            # In this case, we assume exact match from the provided constants
            c_data = sdf[sdf["concept"] == concept_full]
            if not c_data.empty:
                st_metrics[label] = dict(zip(c_data["quarter"], c_data["numeric_value"]))
        key_metrics[stype] = st_metrics

    return ExtractSummary(
        ticker=ticker, company_name=company_name, quarters_requested=quarters_requested,
        quarters_cached=quarters_cached, cache_hit=cache_hit, refresh=refresh,
        per_statement=per_statement, key_metrics=key_metrics, available_concepts=available_concepts
    )

def build_query_summary(ticker, statement_type, concept_filter, rows):
    return QuerySummary(ticker=ticker, statement_type=statement_type, concept_filter=concept_filter, rows=rows)

def build_status_summary(ticker, per_statement, available_concepts):
    return StatusSummary(ticker=ticker, per_statement=per_statement, available_concepts=available_concepts)
