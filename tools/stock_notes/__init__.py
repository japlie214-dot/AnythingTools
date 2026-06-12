"""
Stock Notes Tool — SEC EDGAR Filing Footnote Explorer
=====================================================

Explores, extracts, and queries financial footnotes from SEC EDGAR filings
(10-K, 10-Q, 20-F, 6-K) using tidy XBRL data.

Interface:
  command: "discover" | "note" | "details"
  instructions: JSON object with command-specific parameters

Workflow Guide
--------------

Step 1: Discover Filings
    Find the unique accession_no for recent filings.
    command: "discover"
    instructions: {"ticker": "AAPL", "forms": "10-K,10-Q"}
    
    This queries the SEC EDGAR API and returns a table of recent filings,
    including their date, period, and accession_no. No data is stored yet.

Step 2: List Notes in a Filing (The Index Phase)
    Find available notes inside a specific filing and see what concepts they hold.
    command: "note"
    instructions: {"accession_no": "0000320193-26-000013"}
    
    This retrieves a list of all notes (Note 1, Note 2, etc.) along with a
    short preview of the primary XBRL concepts detected within each note.
    Data is cached locally; no re-extraction occurs for the listing view.

Step 3: Open a Specific Note (The "Hydration" Phase)
    Extract the detailed narrative and display the Concept Catalog for a specific note.
    command: "note"
    instructions: {"accession_no": "0000320193-26-000013", "note_number": 6}
    
    HYDRATION DETAILS:
    When you specify a note_number, the system ALWAYS rehydrates: it deletes
    any existing cached data for this filing and re-extracts fresh data from
    SEC EDGAR. This ensures data freshness even if the original filing was
    amended or the previous extraction was interrupted.

    WHY REHYDRATION IS MANDATORY:
    - Amended filings (10-K/A, 10-Q/A) supersede original data
    - Previous extractions may have been interrupted by network errors
    - Schema evolution requires re-extraction in the current format
    - SEC EDGAR occasionally revises filing data post-submission

    WHAT HAPPENS DURING REHYDRATION:
    1. All existing rows for this accession_no are deleted from:
       - sn_note_details (tidy data rows)
       - sn_detail_registry (concept catalog metadata)
       - sn_notes (note metadata and narratives)
    2. Filing data is re-extracted from SEC EDGAR
    3. Fresh data is written to both Operational and Cloud Backup databases
    4. The Concept Catalog is rebuilt from the new data

    CONCEPT CATALOG:
    The catalog shows up to 50 queryable XBRL concepts with:
    - Concept name (e.g., us-gaap_LongTermDebt)
    - Human-readable label (e.g., Long-Term Debt)
    - Dimension axis and member for dimensional breakdowns
    - Period count and date range coverage
    - Quick Query templates for copy-paste into the details command

    TROUBLESHOOTING:
    - If rehydration fails with a connection error, retry after a few minutes
    - If rehydration fails with a rate limit error, wait 1+ minute before retrying
    - If data appears incorrect, verify the accession_no matches the intended
      filing (amended filings have different accession numbers)

Step 4: Query Details by Concept
    Extract historical time-series data (up to 500 rows) across filings for a
    specific XBRL concept.
    command: "details"
    instructions: {"ticker": "AAPL", "concept": "us-gaap_LongTermDebt", "start_date": "2024-09", "end_date": "2026-03"}
    
    The concept name and suggested start/end date ranges are copied directly
    from the Quick Queries shown in Step 3's Concept Catalog.

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
- Rehydration: Always performed when note_number is provided. Listing view uses cache.
- Backup Integration: All writes to sn_note_details, sn_notes, and sn_detail_registry
  are synced to Snowflake via inline dual-write (no read-back pattern).
- Rate Limiting: Synchronous sliding-window rate limiting prevents SEC IP bans.
- Resume Mechanism: Supports note-level resumption during extraction.
- Deterministic PKs: sn_detail_registry uses MD5(ticker|table_name|accession_no|note_number)
  as the primary key to prevent Snowflake MERGE duplicate row errors.
"""

from .tool import StockNotesTool

__all__ = ["StockNotesTool"]
