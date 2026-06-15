"""tools/stock_notes/__init__.py
Stock Notes Tool — SEC EDGAR Footnote Narrative and Tidy Concept Explorer
========================================================================

An advanced footnote extraction and analysis tool that drills into detailed SEC
filing footnotes (10-K, 10-Q, 20-F, 6-K) to isolate narratives, parse embedded data
tables, and expose dimensional XBRL concepts (tidy-format).

API Interface & Command Reference:
----------------------------------
All payloads must follow the nested JobCreateRequest structure, where commands are
passed inside the 'args' field:

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
   Uses local operational database cache by default (cache-first strategy).
   If the filing has never been extracted, it will be auto-extracted from EDGAR.

   Example Payload:
   {
     "command": "note",
     "instructions": {
       "accession_no": "0000320193-26-000013"
     }
   }

3. COMMAND: "note" (Drill-Down / Hydration View)
   Specify a 'note_number' to unpack a note's full text and generate a queryable
   Concept Catalog. If the note has never been hydrated (no detail records exist),
   it will be auto-hydrated from EDGAR on first access.

   Example Payload:
   {
     "command": "note",
     "instructions": {
       "accession_no": "0000320193-26-000013",
       "note_number": 6
     }
   }

   *Conditional Hydration Mechanics:*
     The system follows a cache-first strategy:

     - **Default behavior (force_refresh: false)**: If the filing or note data has
       already been cached locally, it loads instantly from the database to save
       bandwidth and API calls. Only if no cached data exists will EDGAR be queried.

     - **Force refresh (force_refresh: true)**: Pass `"force_refresh": true` to
       force-purge local records and rehydrate fresh tables from SEC EDGAR. This
       is useful when the original filing was amended or cached data is stale.

     - **Auto-hydration**: When a specific note_number is requested but no detail
       records exist for that note (e.g., the filing was extracted but this
       particular note was not fully hydrated), the system will automatically
       trigger extraction for the missing data without requiring force_refresh.

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

   *Concept Note:* Copy concept names directly from the quick queries displayed
   in Step 3's Concept Catalog.

Troubleshooting and Diagnostics:
-------------------------------
  1. ISSUE: "No data found for concept" or Empty Concept Catalog
     - Resolution: The footnote has not been hydrated. Run the `note` command
       specifying the `accession_no` and `note_number` to hydrate the catalog
       and its tidy rows. Auto-hydration should handle this, but if it fails,
       use `force_refresh: true`.

  2. ISSUE: Amended Filings (10-K/A, 10-Q/A) or Stale Cached Data
     - Resolution: Amended filings have different accession numbers. If you need
       to force-purge the cache for an existing accession number, execute the
       `note` command with `"force_refresh": true`.

  3. ISSUE: Footnote Tables Missing Columns or Corrupted Values
     - Resolution: Footnotes can contain extremely irregular tables with highly
       customized company-specific columns. The extractor converts non-standard
       tables into tidy rows. Use the "details" command to query the flattened
       concept values.

  4. ISSUE: AnythingLLM Integration - "Files not showing up"
     - Resolution: The long narratives are written directly to AnythingLLM's
       `custom-documents/` directory via `artifact_manager.py`. Never try to send
       the raw markdown file as a base64 string attachment; let AnythingLLM ingest
       it natively from the workspace path.

  5. ISSUE: Unique Constraint Violations / duplicate rows on Snowflake Cloud
     - Resolution: `sn_detail_registry` uses an MD5 hash of
       `(ticker, detail_table_name, accession_no, note_number)` as the primary key
       to ensure idempotent, duplicate-free Snowflake merging. If you experience
       reconciliation errors, check '/api/diagnostics'.

  6. ISSUE: Snowflake Binding Error — "Binding data in type (timestamp) is not supported"
     - Resolution: The cloud writer pipeline now automatically converts pandas
       Timestamp objects to Python datetime objects before Snowflake binding. If you
       still encounter this error, ensure you are running the latest version with
       the type_sanitizer module enabled.
"""

from .tool import StockNotesTool

__all__ = ["StockNotesTool"]
