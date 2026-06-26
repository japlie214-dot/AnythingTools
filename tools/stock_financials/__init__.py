# tools/stock_financials/__init__.py
"""
Stock Financials Tool — Quarterly Financial Statement Extractor
=============================================================

Extracts, normalizes, and queries quarterly financial facts (Income Statement,
Balance Sheet, Cash Flow) directly from SEC EDGAR filings as flat, tidy data.

Architecture:
  - **Extractor** (extractor.py): Fetches data from SEC EDGAR via edgartools,
    processes raw XBRL facts into quarterly records, and persists to both the
    local SQLite operational database and Snowflake cloud backup.
  - **Query Engine** (query.py): Reads from the operational database with
    per-share and share-count formatting logic.
  - **Tool** (tool.py): FastAPI async handler bridging HTTP requests to the
    synchronous extractor/query engine.

Data Flow:
  EDGAR → edgartools → extractor.py → SQLite (local) + cloud_writer (Snowflake)
                                                    ↓
                                              query.py ← tool.py ← HTTP

Interface:
  command: "extract" | "query" | "status"
  instructions: JSON object with command-specific parameters

Workflow Guide
--------------

Step 1: Extract Financial Data
    Fetch quarters from SEC EDGAR for a given ticker.
    command: "extract"
    instructions: {"ticker": "NVDA", "quarters": 8, "refresh": false}

    *Cache-First Strategy:*
    - **Default behavior (refresh: false)**: If the requested number of quarters
      is already cached in the operational database, extraction is skipped entirely.
      The tool returns the count of cached quarters without hitting EDGAR.
    - **Force refresh (refresh: true)**: Deletes all existing cached quarters for
      this ticker from both the local database and Snowflake, then re-extracts
      from EDGAR. This is useful when filings have been amended or data appears
      stale.
    - The `quarters` parameter is capped at 40 to prevent excessive EDGAR API usage.

Step 2: Query Financial Data
    Query the cached, operational database for specific financial facts.
    command: "query"
    instructions: {
      "ticker": "NVDA",
      "statement_type": "income",
      "concept": "us-gaap:Revenue",
      "start_quarter": "2024-Q1",
      "end_quarter": "2026-Q1",
      "limit": 100
    }

    *Statement Types:* income, balance, cashflow
    *Concept Format:* Use the raw SEC EDGAR XBRL notation `us-gaap:<ConceptName>`
      (e.g., `us-gaap:Revenue`). This matches the canonical FASB US-GAAP taxonomy
      and the format used by the `stock_notes` tool.
      Reference: https://xbrl.org/guidance/xbrl-glossary
    *Results:* Returns a JSON summary object (NOT an inline pivot table). The full
      fact records are offloaded to a JSON artifact file; the return value contains
      metadata so you can decide whether to read the artifact. Shape:
      {
        "ticker": "NVDA",
        "statement_type": "income",
        "concept_filter": "us-gaap:Revenues" | null,
        "fact_count": 8,
        "artifact_path": "/abs/path/to/stock_financials/<job_id>/NVDA_income_facts.json" | null,
        "quarters": ["2024-Q1", "2024-Q2", ...],
        "concepts": ["us-gaap:Revenues", ...]
      }
      - fact_count: number of fact records matching the query.
      - artifact_path: absolute path to the JSON file containing the full fact
        records (array of objects with ticker, statement_type, concept, quarter,
        value, unit, etc.). null if the artifact write failed — use fact_count
        to confirm data exists.
      - quarters: sorted list of distinct quarters in the result set.
      - concepts: sorted list of distinct concepts in the result set.
      If no records match, returns {"error": "No records found for <ticker> <statement_type>."}.

Step 3: Check Extraction Status
    Check what quarters are currently stored in the operational database.
    command: "status"
    instructions: {"ticker": "NVDA"}

    Returns a breakdown by statement_type showing quarter count and latest quarter.

Schema:
-------
The `sf_quarterly_facts` table uses a composite primary key of
(ticker, statement_type, concept, quarter) for idempotent upserts.

{
  "type": "object",
  "properties": {
    "command": {
      "type": "string",
      "description": "REQUIRED: One of 'extract', 'query', 'status'."
    },
    "instructions": {
      "type": "object",
      "description": "REQUIRED: JSON object matching the requested command parameters."
    }
  },
  "required": ["command", "instructions"]
}

Troubleshooting and Diagnostics:
--------------------------------
  1. ISSUE: Snowflake error — "Binding data in type (timestamp) is not supported"
     - Root Cause: The Snowflake Python Connector does not support pandas.Timestamp
       objects in parameterized queries. The cloud writer pipeline now automatically
       converts these via the type_sanitizer module.
     - Resolution: Ensure you are running the latest version. The sanitizer converts
       pandas.Timestamp → datetime.datetime before Snowflake binding.
     - Reference: https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-example

  2. ISSUE: Extraction succeeds but no data appears in Snowflake
     - Root Cause: The cloud writer operates on a best-effort, fire-and-forget basis.
       If Snowflake is temporarily unavailable, writes are retried once and then
       routed to the Dead Letter Queue (DLQ).
     - Resolution: Check `/api/diagnostics` for DLQ entries. The periodic SyncEngine
       will catch missed rows during the next sync cycle.

  3. ISSUE: Missing Q4 data for income statement or cash flow
     - Root Cause: Many companies report Q4 as a derived value (FY - Q3). The
       extractor automatically derives Q4 from annual (FY) and YTD-Q3 data when
       available, but this requires both FY and Q3 data to be present in EDGAR.
     - Resolution: Request more quarters to ensure FY data is included.

  4. ISSUE: Per-share values show unexpected magnitude
     - Root Cause: Per-share metrics (e.g., EPS, dividends per share) use units
       like "USD per share" and are formatted with appropriate decimal places.
       Raw numeric values may appear as 0.25 or 0.26 rather than $0.25.
     - Resolution: Use the query command which applies per-share formatting.
"""

from .tool import StockFinancialsTool

__all__ = ["StockFinancialsTool"]
