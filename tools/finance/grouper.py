# tools/finance/grouper.py
"""
Self-correcting AI engine for generating and validating financial SQL formulas.
Implements a multi-attempt loop with AI Debugger sub-agent feedback.
"""
import asyncio, re, sqlite3
import numpy as np
import pandas as pd
from typing import Optional, Dict, Any, Callable

import config
from clients.llm import LLMRequest
from clients.llm.providers.azure import AzureProvider
from utils.logger import get_dual_logger, clear_sql_log

log = get_dual_logger(__name__)
import json

_llm = AzureProvider()


def _execute_sql(db_path: str, ticker: str, sql: str, dates: list[str]) -> pd.DataFrame:
    """Run the candidate SQL and pivot results to (metric x date) format."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            params = (ticker.upper(),) * sql.count('?')
            df = pd.read_sql_query(sql, conn, params=params)
        finally:
            conn.close()
        if df.empty or not {'period_end_date','value'}.issubset(df.columns):
            return pd.DataFrame()
        metric_col = [c for c in df.columns if c not in ('period_end_date','value')][0]
        pivoted = df.pivot_table(index=metric_col, columns='period_end_date', values='value')
        return pivoted.reindex(columns=dates)
    except Exception as e:
        log.dual_log(
            tag="Finance:Grouper",
            message="SQL execution failed.",
            level="WARNING",
            payload={"sql": sql, "error": str(e)},
            exc_info=e,
        )
        return pd.DataFrame()


def _score_sql(
    db_path: str, ticker: str, sql: str,
    reference_df: pd.DataFrame, dates: list[str]
) -> tuple[float, Dict[str, list], str]:
    """
    Execute SQL and compare against reference_df.
    Returns (score, errors_dict, sql_script).
    """
    errors: Dict[str, list] = {'mismatched': [], 'missing': [], 'sql_errors': []}
    sql_match = re.search(r'```sql\s*(.*?)\s*```', sql, re.DOTALL)
    if not sql_match:
        errors['sql_errors'].append('Response missing ```sql ... ``` block.')
        return 0.0, errors, sql
    clean_sql = sql_match.group(1).strip()

    calc_df = _execute_sql(db_path, ticker, clean_sql, dates)
    if calc_df.empty:
        errors['sql_errors'].append('SQL produced no rows — syntax or join error.')
        return 0.0, errors, clean_sql

    ref_aligned = reference_df.reindex(columns=dates).fillna(np.nan)
    match_count, total = 0, len(ref_aligned)

    for group, ref_series in ref_aligned.iterrows():
        if group not in calc_df.index:
            errors['missing'].append(str(group))
            continue
        ref_num = pd.to_numeric(ref_series, errors='coerce')
        calc_num = pd.to_numeric(calc_df.loc[group], errors='coerce')
        ok = True
        for d in dates:
            rv, cv = ref_num.get(d, np.nan), calc_num.get(d, np.nan)
            if pd.isna(rv): continue
            if pd.isna(cv) or not np.isclose(
                rv, cv, rtol=config.VALIDATION_RELATIVE_THRESHOLD, atol=1e-5
            ):
                ok = False
                errors['mismatched'].append({
                    'group': group, 'date': d,
                    'expected': rv, 'calculated': cv
                })
                break
        if ok:
            match_count += 1

    score = match_count / total if total else 0.0
    return score, errors, clean_sql


async def _get_debug_suggestion(errors: Dict, attempt: int) -> str:
    """Call mini-LLM to produce a tactical correction hint from validation errors."""
    if attempt < 2:
        return ''
    from tools.finance.finance_prompts import FINANCE_DEBUG_SUGGESTION_PROMPT
    error_summary = str(errors)[:3000]
    prompt = FINANCE_DEBUG_SUGGESTION_PROMPT.format(error_summary=error_summary)
    resp = await _llm.complete_chat(LLMRequest(
        messages=[{'role':'user','content':prompt}],
        model=config.AZURE_MINI_DEPLOYMENT,
    ))
    match = re.search(r'<debugging_suggestion>(.*?)</debugging_suggestion>', resp.content, re.DOTALL)
    return match.group(1).strip() if match else ''


async def run_grouping_loop(
    db_path: str,
    ticker: str,
    statement_type: str,
    reference_df: pd.DataFrame,
    dates_for_validation: list[str],
    pivoted_raw_df: pd.DataFrame,
    prompt_func: Callable[..., str],
    prompt_args: Dict[str, Any],
) -> Optional[tuple[str, float]]:
    """
    Main control loop.  Returns (best_sql, best_score) or None on total failure.
    """
    best_score = -1.0
    best_sql: Optional[str] = None
    current_args = {**prompt_args}

    try:
        clear_sql_log(statement_type)
    except Exception:
        pass

    for attempt in range(1, config.FINANCE_MAX_ATTEMPTS + 1):
        tag = f"Finance:Grouper:Attempt-{attempt}"
        log.dual_log(
            tag=tag,
            message=f"[{statement_type}] Attempt {attempt}/{config.FINANCE_MAX_ATTEMPTS}",
            payload={"ticker": ticker},
            destination=statement_type,
        )

        # Inject debugger suggestion from attempt 2 onwards
        if attempt > 1:
            suggestion = await _get_debug_suggestion(current_args.get('errors', {}), attempt)
            current_args['debugging_suggestion'] = suggestion

        prompt = prompt_func(**current_args)
        log.dual_log(
            tag=tag,
            message="LLM prompt generated.",
            level="DEBUG",
            payload=prompt,
            destination=statement_type,
        )
        resp = await _llm.complete_chat(LLMRequest(
            messages=[{'role':'user','content':prompt}],
            model=config.AZURE_MINI_DEPLOYMENT,
        ))

        if not resp.content:
            log.dual_log(
                tag=tag,
                message="LLM returned empty response.",
                level="WARNING",
                destination=statement_type,
            )
            current_args['errors'] = {'sql_errors': ['LLM returned empty response.']}
            await asyncio.sleep(3)
            continue

        log.dual_log(
            tag=tag,
            message="LLM response received.",
            level="DEBUG",
            payload=resp.content,
            destination=statement_type,
        )

        score, errors, sql = _score_sql(
            db_path, ticker, resp.content, reference_df, dates_for_validation
        )

        log.dual_log(
            tag=tag,
            message=f"[{statement_type}] Score: {score:.2%}",
            payload={"score": score, "errors": errors, "sql": sql},
            destination=statement_type,
        )

        if score > best_score:
            best_score, best_sql = score, sql

        if score >= config.FINANCE_SUCCESS_THRESHOLD:
            log.dual_log(
                tag=tag,
                message=f"[{statement_type}] SUCCESS at attempt {attempt}.",
                payload={"final_score": score, "sql": sql},
                destination=statement_type,
            )
            return best_sql, best_score

        current_args['errors'] = errors
        current_args['previous_attempt_sql'] = sql
        await asyncio.sleep(2)

    log.dual_log(
        tag="Finance:Grouper:Final",
        message=f"[{statement_type}] Max attempts reached. Best score: {best_score:.2%}.",
        level="WARNING",
        destination=statement_type,
    )
    if best_sql:
        log.dual_log(
            tag="Finance:Grouper:Final",
            message="Returning best-effort SQL.",
            payload={"score": best_score, "sql": best_sql},
            destination=statement_type,
        )
    else:
        log.dual_log(
            tag="Finance:Grouper:Final",
            message="No successful SQL candidates generated.",
            level="WARNING",
            destination=statement_type,
        )
    return (best_sql, best_score) if best_sql else None
