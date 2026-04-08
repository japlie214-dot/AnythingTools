# tools/finance/reconciler.py
"""
Financial Reconciliation Logic

Isolates the AI-driven SQL generation and validation logic for financial
fundamentals analysis. Handles both raw fundamentals (SEC EDGAR) and
reference data (YFinance).
"""

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, List, Optional

import pandas as pd
import numpy as np

from clients.llm import get_llm_client, LLMRequest
from database.connection import DatabaseManager
from database.writer import enqueue_write
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)
import config


class FinancialReconciler:
    """
    Handles the complex logic of generating and validating SQL queries for
    financial analysis against raw and normalized financial data.
    """
    
    def __init__(self, ticker: str, statement_type: str):
        self.ticker = ticker.upper()
        self.statement_type = statement_type
        self.reference_values: Dict[str, float] = {}
        self.best_score: float = -1.0
        self.best_sql: Optional[str] = None
        self.last_errors: Dict[str, Any] = {}
        
    def set_reference_values(self, reference_values: Dict[str, float]) -> None:
        """Set reference YFinance data for validation."""
        self.reference_values = reference_values
    
    def _detect_and_apply_scale(self, expected: float, calculated: float) -> float:
        """Determines if discrepancy is purely a unit scaling issue and adjusts."""
        try:
            if pd.isna(expected) or pd.isna(calculated) or expected == 0 or calculated == 0:
                return calculated
        except Exception:
            return calculated

        ratio = abs(expected / calculated)

        # Typical thresholds with some tolerance
        if 900 < ratio < 1100:
            return calculated * 1000
        if 900000 < ratio < 1100000:
            return calculated * 1_000_000
        # Inverse directions
        if 0.0009 < ratio < 0.0011:
            return calculated / 1000
        if 0.0000009 < ratio < 0.0000011:
            return calculated / 1_000_000

        return calculated
    
    def _validate_sql_scored(
        self, 
        generated_sql: str,
        reference_values: Dict[str, float]
    ) -> Tuple[float, Dict[str, Any], str]:
        """
        Execute generated SQL against local DB and score its accuracy.
        
        Returns:
            score: Ratio of matched values to total reference values
            errors: Type-specific error details
            sql: Cleaned SQL string
        """
        errors = {
            "sql_errors": [],
            "mismatched": [],
            "missing": [],
        }
        
        try:
            from database.reader import execute_read_sql, ReaderError
            from database.writer import wait_for_writes
            import asyncio

            # If this call is running in an async context, the caller should await wait_for_writes() before invoking.
            try:
                asyncio.get_running_loop()
                # We are in the event loop thread. Blocking here causes a deadlock.
                # Skip sync-wait; rely on the async caller to have awaited wait_for_writes() if freshness is needed.
            except RuntimeError:
                # No running loop: wait synchronously
                import asyncio as _asyncio
                _asyncio.run(wait_for_writes())
            except Exception:
                log.dual_log(tag="Finance:Reconciler", message="wait_for_writes() failed or timed out — proceeding without guaranteed freshness.", level="WARNING")

            dict_rows = execute_read_sql(generated_sql)
            rows = {}
            for r in dict_rows:
                vals = list(r.values())
                if len(vals) >= 2:
                    rows[str(vals[0])[:10]] = float(vals[1])
        except Exception as exc:
            errors['sql_errors'].append(str(exc))
            return 0.0, errors, generated_sql

        if not rows:
            errors['sql_errors'].append('SQL executed but returned no rows.')
            return 0.0, errors, generated_sql

        match, total = 0, len(reference_values)
        for date, ref_val in reference_values.items():
            if date not in rows:
                errors['missing'].append(date)
                continue
            
            calc_val = rows[date]
            calc_val_scaled = self._detect_and_apply_scale(ref_val, calc_val)

            if np.isclose(ref_val, calc_val_scaled, rtol=config.VALIDATION_RELATIVE_THRESHOLD, atol=1e-5):
                match += 1
            else:
                errors['mismatched'].append({
                    'date': date,
                    'expected': ref_val,
                    'calculated': calc_val,
                    'scaled_calculated': calc_val_scaled if calc_val_scaled != calc_val else None,
                })

        score = match / total if total > 0 else 0.0
        return score, errors, generated_sql
    
    def _format_validation_errors(self, errors: Dict[str, Any]) -> str:
        """Render typed validation errors into prompt-ready format."""
        lines = []
        for msg in errors.get('sql_errors', []):
            lines.append(f'SQL ERROR: {msg}')
        for m in errors.get('mismatched', []):
            expected = m.get('expected')
            calculated = m.get('calculated')
            if expected is not None and calculated is not None:
                lines.append(
                    f'MISMATCH on {m["date"]}: '
                    f'expected={m["expected"]:.2f}, got={m["calculated"]:.2f}'
                )
            else:
                lines.append(f'MISMATCH RECORD: {m}')
        for date in errors.get('missing', []):
            lines.append(f'MISSING date in output: {date}')
        return '\n'.join(lines) if lines else 'No errors (unknown failure).'
    
    def _build_prompt(
        self,
        error_context: str = '',
        debugging_suggestion: str = '',
        use_raw_fundamentals: bool = False
    ) -> str:
        """
        Build AI prompt for SQL generation.
        """
        from tools.finance.finance_prompts import build_reconciler_prompt
        return build_reconciler_prompt(
            self.ticker,
            self.statement_type,
            self.reference_values,
            error_context,
            debugging_suggestion,
            use_raw_fundamentals
        )
    
    async def _get_debugger_suggestion(
        self,
        errors: Dict[str, Any],
        previous_sql: str
    ) -> str:
        """
        Calls a lightweight debugger-AI to diagnose validation failures.
        """
        if not any(errors.values()):
            return ''

        from tools.finance.finance_prompts import FINANCE_RECONCILER_DEBUG_PROMPT
        prompt = FINANCE_RECONCILER_DEBUG_PROMPT.format(
            ticker=self.ticker,
            errors_json=json.dumps(errors, indent=2),
            previous_sql=previous_sql
        )
        
        llm_mini = get_llm_client(provider_type='azure')
        response = await llm_mini.complete_chat(
            LLMRequest(
                messages=[{'role': 'user', 'content': prompt}],
                model=config.AZURE_MINI_DEPLOYMENT,
            )
        )
        
        match = re.search(
            r'<debugging_suggestion>(.*?)</debugging_suggestion>',
            response.content, re.DOTALL
        )
        return match.group(1).strip() if match else ''
    
    def _get_validated_formula(
        self,
        reference_values: Dict[str, float]
    ) -> Tuple[str, float] | None:
        """
        Returns cached SQL if it still passes validation threshold.
        """
        try:
            from database.reader import execute_read_sql
            rows = execute_read_sql(
                'SELECT sql_query, validation_score FROM financial_formulas'
                ' WHERE ticker = ? AND statement_type = ?'
                ' ORDER BY validated_at DESC LIMIT 1',
                (self.ticker, self.statement_type),
            )
        except Exception:
            return None

        if not rows:
            return None

        row = rows[0]
        cached_sql, cached_score = row['sql_query'], row['validation_score']

        # Re-validate against current reference data
        live_score, _, _ = self._validate_sql_scored(
            cached_sql, reference_values
        )

        if live_score >= config.FORMULA_VALIDATION_THRESHOLD:
            return cached_sql, live_score

        log.dual_log(
            tag="Finance:Reconciler",
            message=f"Cached formula for {self.ticker}/{self.statement_type} is stale "
                    f"(live score {live_score * 100:.1f}%). Regenerating.",
            level="WARNING",
        )
        return None
    
    async def generate_validated_sql(
        self,
        max_attempts: int = 5,
        use_raw_fundamentals: bool = False
    ) -> Tuple[str, float]:
        """
        Generate and validate SQL query using agentic approach.
        
        Returns:
            final_sql: The best validated SQL query
            final_score: Validation score (0.0 to 1.0)
        """
        # Check for cached validated formula first
        existing_data = self._get_validated_formula(self.reference_values)
        if existing_data:
            existing_sql, existing_score = existing_data
            self.best_sql = existing_sql
            self.best_score = existing_score
            return existing_sql, existing_score
        
        # Initialize LLM
        llm = get_llm_client(provider_type="azure")
        
        best_score: float = -1.0
        best_sql: Optional[str] = None
        error_feedback: str = ''
        debugging_suggestion: str = ''
        
        for attempt in range(1, max_attempts + 1):
            prompt = self._build_prompt(
                error_feedback, 
                debugging_suggestion,
                use_raw_fundamentals
            )
            
            log.dual_log(
                tag=f"Finance:Reconciler:Attempt-{attempt}",
                message="Generated prompt.",
                level="DEBUG",
                payload=prompt,
                destination=self.statement_type,
            )
            
            response = await llm.complete_chat(
                LLMRequest(
                    messages=[{'role': 'user', 'content': prompt}]
                )
            )
            
            # Extract SQL from response
            sql_match = re.search(r'```sql\s*(.*?)\s*```', response.content, re.DOTALL | re.IGNORECASE)
            generated_sql = (sql_match.group(1).strip() if sql_match
                             else response.content.strip().replace('```', ''))
            
            if not generated_sql:
                error_feedback = 'No SQL was extracted from the model response.'
                continue
            
            # Validate
            score, errors, validated_sql = self._validate_sql_scored(
                generated_sql, self.reference_values
            )
            
            if score > best_score:
                best_score = score
                best_sql = validated_sql
            
            if score >= config.FINANCE_SUCCESS_THRESHOLD:
                break
            
            error_feedback = self._format_validation_errors(errors)
            
            if attempt > 1:
                suggestion = await self._get_debugger_suggestion(errors, validated_sql)
                if suggestion:
                    debugging_suggestion = suggestion
                    error_feedback += f'\n\nDebugger directive: {suggestion}'
            
            await asyncio.sleep(2)
        
        if not best_sql:
            raise ValueError(f'Could not generate accurate SQL for {self.ticker}')
        
        if best_score < config.FINANCE_SUCCESS_THRESHOLD:
            log.dual_log(
                tag="Finance:Reconciler:Final",
                message="Best-effort SQL below threshold.",
                level="WARNING",
                payload={"ticker": self.ticker, "score": best_score},
            )
        
        # Cache the validated formula
        enqueue_write(
            'INSERT OR REPLACE INTO financial_formulas '
            '(ticker, statement_type, sql_query, validation_score, validated_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (self.ticker, self.statement_type, best_sql, best_score, self._utcnow()),
        )
        
        self.best_sql = best_sql
        self.best_score = best_score
        
        return best_sql, best_score
    
    def format_results(self) -> str:
        """Format the results for display."""
        if not self.best_sql:
            return f"❌ No valid SQL generated for {self.ticker}"
        
        rows = list(self.reference_values.items())[:4]
        lines = [
            f"### 💾 Financial Analysis: {self.ticker} — {self.statement_type}",
            "*(Reconciled reference data & Self-Corrected SQL Formula)*\n",
            "| Period End | Value |",
            "|---|---|",
        ]
        
        for date, val in rows:
            lines.append(f"| {date} | ${val:,.2f} |")
        
        lines.append(
            f"\n**Validated SQL rule (preview):**\n```sql\n{self.best_sql[:200]}\n```"
        )
        lines.append(f"\n**Validation Score:** {self.best_score:.2%}")
        
        return "\n".join(lines)
    
    @staticmethod
    def _utcnow() -> str:
        """Return current UTC time as ISO 8601 string."""
        return datetime.now(timezone.utc).isoformat()


# Module-level functions for backward compatibility
async def validate_sql_scored(
    generated_sql: str,
    ticker: str,
    reference_values: Dict[str, float],
) -> Tuple[float, Dict[str, Any], str]:
    """Legacy function wrapper."""
    reconciler = FinancialReconciler(ticker, "Unknown")
    return reconciler._validate_sql_scored(generated_sql, reference_values)


def format_validation_errors(errors: Dict[str, Any]) -> str:
    """Legacy function wrapper."""
    reconciler = FinancialReconciler("Unknown", "Unknown")
    return reconciler._format_validation_errors(errors)


def detect_and_apply_scale(expected: float, calculated: float) -> float:
    """Legacy function wrapper."""
    reconciler = FinancialReconciler("Unknown", "Unknown")
    return reconciler._detect_and_apply_scale(expected, calculated)


