# tools/stock_notes/__init__.py
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

Step 3: Open a Specific Note (The "Hydration" Phase)
    Extract the detailed narrative and display the Concept Catalog for a specific note.
    command: "note"
    instructions: {"accession_no": "0000320193-26-000013", "note_number": 6}
    
    HYDRATION DETAILS:
    To view a specific note's Concept Catalog, you must supply a `note_number`.
    The system will parse the note, build a Concept Catalog (up to 50 concepts),
    and render sample values and recommended date ranges.
    
    TROUBLESHOOTING & REHYDRATION:
    If a subsequent query returns "No records found" or if you suspect data has
    drifted (such as after an amended 10-K/A is filed), add `"force_refresh": true`
    to your instructions. This forces a clean deletion of the local cache and
    re-extracts fresh data from EDGAR.

Step 4: Query Details by Concept
    Extract historical time-series data (up to 12 quarters) across filings for a
    specific XBRL concept.
    command: "details"
    instructions: {"ticker": "AAPL", "concept": "us-gaap_LongTermDebt", "start_date": "2024-09", "end_date": "2026-03"}
    
    The concept name and suggested start/end date ranges are copied directly from the
    Concept Catalog displayed in Step 3.

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
- Rehydration: The 'note' command supports an optional force_refresh flag to wipe local cache and re-extract.
- Backup Integration: All writes to sn_note_details, sn_notes, and sn_detail_registry are synced to Snowflake.
- Rate Limiting: Synchronous sliding-window rate limiting prevents SEC IP bans.
- Resume Mechanism: Supports note-level resumption during extraction.
"""

from .tool import StockNotesTool

__all__ = ["StockNotesTool"]
