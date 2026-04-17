# deprecated/tools/finance/finance_prompts.py

import json

FINANCE_DEBUG_SUGGESTION_PROMPT = """You are a SQLite debugging expert.
Provide a single, concise tactical suggestion (2 to 3 sentences) on how to fix the SQL.
Wrap your suggestion in <debugging_suggestion> tags.

### ERROR SUMMARY
{error_summary}
###
<debugging_suggestion>"""

FINANCE_RECONCILER_DEBUG_PROMPT = """You are a SQL debugging expert.
Provide exactly one concise tactical fix directive on how to fix the failing SQL query for ticker {ticker}.
Wrap your suggestion in <debugging_suggestion> tags.

Example: <debugging_suggestion>Cast the date column to TEXT[:10] before comparing to reference keys.</debugging_suggestion>

### VALIDATION ERRORS
{errors_json}

### FAILING SQL
{previous_sql}
###
<debugging_suggestion>"""


def build_reconciler_prompt(ticker: str, statement_type: str, reference_values: dict, error_context: str = '', debugging_suggestion: str = '', use_raw_fundamentals: bool = False) -> str:
    base = (
        f'Generate a SQLite query to calculate "{statement_type}" '
        f'for ticker "{ticker}".\n'
        f'Reference values (date â†’ expected value):\n'
        f'{json.dumps(reference_values, indent=2)}\n\n'
        f'Rules:\n'
        f'- Return ONLY valid SQL â€” no prose, no Markdown fences.\n'
        f'- Date keys are ISO-format strings truncated to 10 chars (YYYY-MM-DD).\n'
        f'- Output must contain one row per reference date.\n'
    )
    
    if use_raw_fundamentals:
        base += (
            f'\nDATA SOURCE: Use raw SEC EDGAR fundamentals from `raw_fundamentals` table.\n'
            f'SCHEMA: raw_fundamentals(ticker, statement_type, period_end_date, label, concept, value, unit, source)\n'
            f'RECOMMENDED APPROACH: Use CTE with window functions to aggregate quarterly values.\n'
            f'EXAMPLE CONCEPTS: Look for concepts containing "Revenue", "Net Income", "Assets", etc.\n'
        )
    else:
        base += (
            f'\nDATA SOURCE: Use existing financial_formulas table OR build from scratch.\n'
            f'If building from scratch, you may reference raw fundamentals table.\n'
        )
    
    if error_context:
        base += (
            f'\nYOUR PREVIOUS ATTEMPT FAILED.\n'
            f'Validation errors:\n{error_context}\n'
        )
    if debugging_suggestion:
        base += f'\nExpert directive: {debugging_suggestion}\n'
    
    return base



def build_finance_tool_grouping_prompt(statement_type, reference_df, raw_df, errors=None, previous_attempt_sql=None, debugging_suggestion='', **kwargs):
    ref_str = reference_df.to_string() if not reference_df.empty else 'N/A'
    raw_str = raw_df.to_string()[:4000] if not raw_df.empty else 'N/A'
    err_str = str(errors) if errors else 'None'
    debug_str = f'\nDEBUGGER HINT: {debugging_suggestion}' if debugging_suggestion else ''
    prev_str = f'\nPREVIOUS SQL (failed):\n{previous_attempt_sql}' if previous_attempt_sql else ''
    return (
        f'You are a financial SQL expert.\n'
        f'Generate a SQLite query against raw_fundamentals (columns: ticker, statement_type, '
        f'period_end_date, label, concept, value) that produces: '
        f'period_end_date, metric_name, value for {statement_type}.\n\n'
        f'INSTRUCTIONS:\n'
        f'1. Output exclusively a SQLite query.\n'
        f'2. Use ? for the ticker parameter.\n'
        f'3. Enclose the query in ```sql ... ``` fences.\n\n'
        f'### TARGET METRICS (YFINANCE REFERENCE)\n{ref_str}\n\n'
        f'### AVAILABLE RAW CONCEPTS (SAMPLE)\n{raw_str}\n\n'
        f'### ERRORS FROM PREVIOUS ATTEMPT\n{err_str}{debug_str}{prev_str}\n###\n```sql\nSELECT'
    )

