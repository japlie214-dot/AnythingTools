# tools/finance/tool.py
"""
Financial fundamentals tool - retrieves YFinance data and reconciles with AI-generated SQL, with SEC EDGAR support.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import yfinance as yf

from tools.base import BaseTool, TelemetryCallback
from utils.logger import get_dual_logger
import config

log = get_dual_logger(__name__)

# Use relative imports for intra-package modules
from .pipeline import run_financial_pipeline
from .ingestion import ingest_sec_fundamentals, query_fundamentals


async def _validate_ticker(ticker: str) -> tuple[bool, dict]:
    """
    Lightweight validity guard using yfinance.info.
    Returns (is_valid, info_dict).
    A ticker is considered valid if yfinance returns a non-empty info dict
    with at least one of: trailingPE, marketCap, previousClose.
    """
    def _fetch():
        try:
            info = yf.Ticker(ticker).info
            return info
        except Exception:
            return {}

    info = await asyncio.to_thread(_fetch)
    validity_fields = ['trailingPE', 'marketCap', 'previousClose', 'regularMarketPrice']
    is_valid = any(info.get(f) is not None for f in validity_fields)
    return is_valid, info


class FinanceTool(BaseTool):
    name = "finance"
    
    def is_resumable(self, args: dict[str, Any]) -> bool:
        """Return True if the tool supports mid-run resume for the given args."""
        return args.get("action") == "ingest"
    
    async def run(self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs) -> str:
        """
        Enhanced finance tool that supports:
        1. Standard YFinance + SQL reconciliation
        2. SEC EDGAR ingestion with granular fundamentals
        3. Querying raw fundamentals data
        """
        dry_run = kwargs.get('dry_run', config.TELEGRAM_DRY_RUN)
        if dry_run:
            log.dual_log(tag="Finance:Tool", message=f'[DRY RUN] Would execute finance tool for ticker {args.get("ticker", "").upper()}', level="INFO", payload={'event_type': 'finance.dry_run'})
            return "[DRY RUN] Finance tool execution skipped."

        ticker = args.get("ticker", "").strip().upper()
        if not ticker:
            return "❌ No ticker symbol provided. Usage: finance {\"ticker\": \"AAPL\"}"

        await telemetry(self.status(f'Validating ticker {ticker}...', 'RUNNING'))
        is_valid, yf_info = await _validate_ticker(ticker)
        if not is_valid:
            return (
                f'❌ Ticker <b>{ticker}</b> could not be validated.\n'
                f'It may be delisted, invalid, or a non-US security not covered by yfinance.\n'
                f'Please verify the ticker symbol and try again.'
            )
        await telemetry(self.status(f'Ticker {ticker} validated.', 'RUNNING'))

        action = args.get("action", "analyze")  # analyze, ingest, or query
        statement = args.get("statement", "Quarterly Earnings")

        # ACTION 1: INGEST - Fetch and store raw SEC EDGAR fundamentals
        if action == "ingest":
            return await self._handle_ingest(ticker, statement, telemetry)

        # ACTION 2: QUERY - Query existing raw fundamentals
        if action == "query":
            concept = args.get("concept", "")
            start = args.get("start", "2020-01-01")
            end = args.get("end", "2025-12-31")
            return await self._handle_query(ticker, concept, start, end)

        # ACTION 3: ANALYZE - Standard YFinance + SQL reconciliation (default)
        return await self._handle_analyze(ticker, statement, telemetry)

    async def _handle_analyze(self, ticker: str, statement: str, telemetry: TelemetryCallback) -> str:
        """Use the new freshness-aware pipeline instead of direct reconciler."""
        await telemetry(self.status(f"Running financial pipeline for {ticker}…", "RUNNING"))
        
        try:
            # Call the pipeline which handles freshness checks and reconciliation
            results = await run_financial_pipeline(
                ticker=ticker,
                force_refresh=False,  # Could be made configurable via args
                num_quarters=12
            )
            
            if not results:
                return f"No financial data could be reconciled for {ticker}."
            
            # Format results
            lines = [f"### Financial Summary: {ticker}"]
            for st, data in results.items():
                score = data.get("score", 0)
                lines.append(f"**{st}**: Validation Score {score:.1%}")
            
            await telemetry(self.status("Pipeline completed successfully", "SUCCESS"))
            return "\n".join(lines)
            
        except Exception as e:
            await telemetry(self.status(f"Pipeline failed: {str(e)}", "ERROR"))
            return f"❌ Pipeline failed: {str(e)}"

    async def _handle_ingest(self, ticker: str, statement: str, telemetry: TelemetryCallback) -> str:
        """Ingest raw SEC EDGAR fundamentals."""
        await telemetry(self.status(f"Starting SEC EDGAR ingestion for {ticker}…", "RUNNING"))
        
        try:
            result = await ingest_sec_fundamentals(ticker, statement, num_quarters=4)
            
            if result["status"] == "success" or result["status"] == "success (mock)":
                await telemetry(self.status(
                    f"✅ Ingestion complete: {result['records_inserted']} records inserted", 
                    "SUCCESS"
                ))
                return (
                    f"### ✅ SEC EDGAR Ingestion Complete\n"
                    f"**Ticker:** {ticker}\n"
                    f"**Statements:** {', '.join(result.get('statements_processed', []))}\n"
                    f"**Records:** {result['records_inserted']} inserted\n"
                    f"**Status:** {result['status']}"
                )
            else:
                await telemetry(self.status(f"Ingestion failed: {result['message']}", "ERROR"))
                return f"❌ Ingestion failed: {result['message']}"
                
        except Exception as e:
            await telemetry(self.status(f"Ingestion error: {str(e)}", "ERROR"))
            return f"❌ Ingestion error: {str(e)}"

    async def _handle_query(self, ticker: str, concept: str, start: str, end: str) -> str:
        """Query ingested raw fundamentals."""
        if not concept:
            return "Error: 'concept' parameter required for query action"
        
        log.dual_log(tag="Finance:Tool", message=f"Querying fundamentals: {ticker} {concept} {start}-{end}", level="INFO", payload={"ticker": ticker, "concept": concept, "start": start, "end": end})
        
        results = await query_fundamentals(ticker, concept, start, end)
        
        if not results:
            return f"No results found for {ticker} / {concept} / {start}-{end}"
        
        # Format results
        lines = [
            f"### 📊 Raw Fundamentals Query: {ticker}",
            f"**Concept:** {concept}",
            f"**Period:** {start} to {end}\n",
            "| Date | Statement | Value | Label |",
            "|---|---|---|---|"
        ]
        
        for row in results[:20]:  # Limit to 20 rows for display
            lines.append(
                f"| {row['period_end_date']} | {row['statement_type']} | "
                f"${row['value']:,.2f} | {row['label']} |"
            )
        
        if len(results) > 20:
            lines.append(f"\n... and {len(results) - 20} more rows")
        
        return "\n".join(lines)

    async def _run_ai_grouping_pipeline(
        self,
        ticker: str,
        reference_data: dict,     # {'Income Statement': df, 'Balance Sheet': df, ...}
        raw_df: pd.DataFrame,     # flat raw_fundamentals data for this ticker
        pivoted_raw_df: pd.DataFrame,
        common_dates: list[str],
        db_path: str,
        telemetry,
    ) -> None:
        """
        Validate-first pipeline:
        1. Check existing formula cache
        2. Score existing formula against yfinance reference
        3. Regenerate only stale/missing formulas
        4. Persist updated formulas
        """
        from database.formula_cache import get_formula, save_formula
        from tools.finance.grouper import run_grouping_loop
        from tools.finance.scale_utils import detect_and_apply_scale
        import asyncio

        STATEMENTS = ['Income Statement', 'Balance Sheet', 'Cash Flow']
        tasks_to_regenerate = []

        for st_type in STATEMENTS:
            ref_df = reference_data.get(st_type)
            if ref_df is None or ref_df.empty:
                log.dual_log(tag="Finance:Tool", message=f'No yfinance reference for {st_type}. Skipping.', level="WARNING", payload={"statement_type": st_type})
                continue

            # Scale correction before any validation
            ref_df = detect_and_apply_scale(ref_df, raw_df)

            existing_sql = get_formula(ticker, st_type)
            if existing_sql:
                from tools.finance.grouper import _score_sql
                score, _, _ = _score_sql(db_path, ticker, existing_sql, ref_df, common_dates)
                if score >= config.FORMULA_VALIDATION_THRESHOLD:
                    await telemetry(self.status(
                        f'{st_type} formula valid (score {score:.0%}). Skipping regen.', 'RUNNING'
                    ))
                    continue
                else:
                    await telemetry(self.status(
                        f'{st_type} formula stale (score {score:.0%}). Queuing regen.', 'RUNNING'
                    ))
            else:
                await telemetry(self.status(f'{st_type} formula absent. Queuing generation.', 'RUNNING'))

            tasks_to_regenerate.append({
                'statement_type': st_type,
                'reference_df': ref_df,
                'dates': common_dates,
            })

        if not tasks_to_regenerate:
            await telemetry(self.status('All formulas valid. No regeneration needed.', 'SUCCESS'))
            return

        from tools.finance.finance_prompts import build_finance_tool_grouping_prompt
        def build_prompt(**kwargs):
            kwargs['raw_df'] = pivoted_raw_df
            return build_finance_tool_grouping_prompt(**kwargs)

        # Launch concurrently
        async_tasks = [
            run_grouping_loop(
                db_path=db_path, ticker=ticker,
                statement_type=t['statement_type'],
                reference_df=t['reference_df'],
                dates_for_validation=t['dates'],
                pivoted_raw_df=pivoted_raw_df,
                prompt_func=build_prompt,
                prompt_args={
                    'statement_type': t['statement_type'],
                    'reference_df': t['reference_df'],
                    'raw_df': pivoted_raw_df,
                },
            )
            for t in tasks_to_regenerate
        ]

        results = await asyncio.gather(*async_tasks, return_exceptions=True)

        for i, result in enumerate(results):
            st_type = tasks_to_regenerate[i]['statement_type']
            if isinstance(result, Exception):
                log.dual_log(tag="Finance:Tool", message=f'Grouping task failed for {st_type}: {result}', level="ERROR", payload={"statement_type": st_type, "error": repr(result)})
            elif result:
                sql_str, score = result
                save_formula(ticker, st_type, sql_str, score)
                await telemetry(self.status(f'{st_type} formula saved (score {score:.0%}).', 'SUCCESS'))
            else:
                log.dual_log(tag="Finance:Tool", message=f'No usable SQL generated for {st_type}.', level="WARNING", payload={"statement_type": st_type})

    @staticmethod
    def _utcnow() -> str:
        """Return current UTC time as ISO 8601 string."""
        return datetime.now(timezone.utc).isoformat()
