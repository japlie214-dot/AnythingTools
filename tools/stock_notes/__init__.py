"""tools/stock_notes/__init__.py
Stock Notes Tool — SEC EDGAR Footnote Narrative and Tidy Concept Explorer
========================================================================

An advanced footnote extraction and analysis tool that drills into detailed SEC 
filing footnotes (10-K, 10-Q, 20-F, 6-K) to isolate narratives, parse embedded data 
tables, and expose dimensional XBRL concepts (tidy-format).

API Interface & Command Reference:
----------------------------------
All payloads must follow the nested JobCreateRequest structure, where commands are passed 
inside the 'args' field:

1. COMMAND: "discover"
   Finds and lists recent filings, exposing their accession numbers.
   
   Example Payload:
   {
     "command": "discover",
     "instructions": {
       "ticker": "AAPL",
       "forms": "10-K,10-Q"
     }
   }

2. COMMAND: "note" (Listing Index View)
   Retrieve a list of all notes (Note 1, Note 2, etc.) inside a specific filing.
   This view uses the local operational database cache by default.
   
   Example Payload:
   {
     "command": "note",
     "instructions": {
       "accession_no": "0000320193-26-000013"
     }
   }

3. COMMAND: "note" (Drill-Down / Hydration View)
   Specify a 'note_number' to unpack a note's full text and generate a queryable Concept Catalog.
   
   Example Payload:
   {
     "command": "note",
     "instructions": {
       "accession_no": "0000320193-26-000013",
       "note_number": 6,
       "force_refresh": false
     }
   }
   
   *Conditional Hydration Mechanics:*
     By default, if a note's details have already been cached locally, it loads instantly 
     from the database to save bandwidth. Pass `"force_refresh": true` to force-purge 
     local records and rehydrate fresh tables from SEC EDGAR.

4. COMMAND: "details"
   Extract historical time-series data across filings for a specific XBRL concept.
   
   Example Payload:
   {
     "command": "details",
     "instructions": {
       "ticker": "AAPL",
       "concept": "us-gaap_LongTermDebt",
       "start_date": "2024-09",
       "end_date": "2026-03"
     }
   }
   
   *Concept Note:* Copy concept names directly from the quick queries displayed in Step 3's Concept Catalog.

Troubleshooting and Diagnostics:
-------------------------------
  1. ISSUE: "No data found for concept" or Empty Concept Catalog
     - Resolution: The footnote has not been hydrated. Run the `note` command specifying 
       the `accession_no` and `note_number` to rehydrate the catalog and its tidy rows.
  2. ISSUE: amended Filings (10-K/A, 10-Q/A) or Stale Cached Data
     - Resolution: Amended filings have different accession numbers. If you need to force-purge 
       the cache for an existing accession number, execute the `note` command with 
       `"force_refresh": true`.
  3. ISSUE: Footnote Tables Missing Columns or Corrupted Values
     - Resolution: Footnotes can contain extremely irregular tables with highly customized 
       company-specific columns. The extractor converts non-standard tables into tidy rows. 
       Use the "details" command to query the flattened concept values.
  4. ISSUE: AnythingLLM Integration - "Files not showing up"
     - Resolution: The long narratives are written directly to AnythingLLM's `custom-documents/` 
       directory via `artifact_manager.py`. Never try to send the raw markdown file as a 
       base64 string attachment; let AnythingLLM ingest it natively from the workspace path.
  5. ISSUE: Unique Constraint Violations / duplicate rows on Snowflake Cloud
     - Resolution: `sn_detail_registry` uses an MD5 hash of `(ticker, detail_table_name, accession_no, note_number)` 
       as the primary key to ensure idempotent, duplicate-free Snowflake merging. If you experience 
       reconciliation errors, check '/api/diagnostics'.
"""

from .tool import StockNotesTool

__all__ = ["StockNotesTool"]
