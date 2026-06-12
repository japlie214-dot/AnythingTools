# tools/stock_notes/__init__.py
"""
Stock Notes Tool Documentation
==============================

The `stock_notes` tool explores, extracts, and queries financial footnotes from SEC 
EDGAR filings (10-K, 10-Q, 20-F, 6-K). 

This tool operates via a unified 2-parameter interface:
1. `command`: The action to perform (`discover`, `note`, `details`).
2. `instructions`: A JSON object holding the parameters specific to that command.

Workflow Guide:
---------------
Step 1: Discover Filings
    Find the `accession_no` for recent filings.
    command: "discover"
    instructions: {"ticker": "AAPL", "forms": "10-K,10-Q"}

Step 2: List Notes in a Filing
    Find available notes inside a specific filing.
    command: "note"
    instructions: {"accession_no": "0000320193-24-000123"}
    *Note: The first time this is called on a filing, it extracts and indexes the entire filing automatically.*

Step 3: Extract & Read a Specific Note
    Read the narrative and see available detail tables for a note.
    command: "note"
    instructions: {"accession_no": "0000320193-24-000123", "note_number": 2}

Step 4: Query Details by Concept
    Extract historical time-series data (up to 12 quarters) across filings for a specific XBRL concept found in a note.
    command: "details"
    instructions: {"ticker": "AAPL", "concept": "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax", "start_date": "2023-01", "end_date": "2025-06"}

Schema:
-------
{
  "type": "object",
  "properties": {
    "command": {
      "type": "string",
      "description": "REQUIRED: The action to perform. Must be one of: 'discover', 'note', 'details'."
    },
    "instructions": {
      "type": "object",
      "description": "REQUIRED: A JSON object containing the parameters required for the command."
    }
  },
  "required": ["command", "instructions"]
}

Developer Notes:
----------------
- Resume Mechanism: Supports note-level resumption during the extraction phase. Interrupted 
  extractions automatically resume from the last completed footnote.
- Backup Integration: Raw note payloads are archived as JSON files under the backup directory.
- Rate Limiting: Synchronous sliding-window rate limiting prevents SEC IP bans.
"""

from .tool import StockNotesTool

__all__ = ["StockNotesTool"]
