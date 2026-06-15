# tools/stock_financials/__init__.py
"""
Stock Financials Tool — Quarterly Financial Statement Extractor
=============================================================

Extracts, normalizes, and queries quarterly financial facts (Income Statement,
Balance Sheet, Cash Flow) directly from SEC EDGAR filings as flat, tidy data.

Interface:
  command: "extract" | "query" | "status"
  instructions: JSON object with command-specific parameters

Workflow Guide
--------------

Step 1: Extract Financial Data
    Fetch quarters from SEC EDGAR for a given ticker.
    command: "extract"
    instructions: {"ticker": "NVDA", "quarters": 8, "refresh": false}
    
    If 'refresh' is true, forces deletion of all cached quarters for this ticker
    and re-extracts them from EDGAR.

Step 2: Query Financial Data
    Query the cached, operational database for specific financial facts.
    command: "query"
    instructions: {
      "ticker": "NVDA",
      "statement_type": "income",
      "concept": "us-gaap_Revenue",
      "start_quarter": "2024-Q1",
      "end_quarter": "2026-Q1",
      "limit": 100
    }

Step 3: Check Extraction Status
    Check what quarters are currently stored in the operational database.
    command: "status"
    instructions: {"ticker": "NVDA"}

Schema:
-------
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
"""

from .tool import StockFinancialsTool

__all__ = ["StockFinancialsTool"]
