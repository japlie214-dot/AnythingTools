# tests/inspect_notes.py
import os
import re
import sys
import random
import pandas as pd
from datetime import datetime
from edgar import Company, set_identity

# Ensure pandas has tabulate installed for markdown formatting export
try:
    import tabulate
except ImportError:
    print("[INFO] Installing 'tabulate' package to support pandas DataFrame markdown formatting...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "tabulate"])

def run_diagnostic():
    # 1. Setup Identity (Required by SEC EDGAR)
    identity = "YourNameYourEmail@domain.com"
    if not identity:
        # Fallback default for testing. Replace with your actual SEC credential string.
        identity = "sumanal_developeremail@domain.com"
        print(f"[WARNING] EDGAR_IDENTITY environment variable not set. Using fallback: '{identity}'")
    
    set_identity(identity)

    # 2. Search filings for AAPL
    ticker = "AAPL"
    print(f"Searching filings for {ticker}...")
    company = Company(ticker)

    # 3. Get the latest 10-K filing
    print("Retrieving latest 10-K filing...")
    filings = company.get_filings(form="10-K", amendments=False)
    if not filings:
        print("[ERROR] No 10-K filings found.")
        return

    latest_filing = filings[0]
    print(f"Filing found: {latest_filing.form} | Accession: {latest_filing.accession_no} | Date: {latest_filing.filing_date}")

    # 4. Parse the filing object and retrieve notes
    print("Parsing filing object (this may take a few seconds)...")
    filing_obj = latest_filing.obj()
    
    if not hasattr(filing_obj, "notes") or not filing_obj.notes:
        print("[ERROR] Filing does not contain parseable footnotes collection.")
        return

    all_notes = list(filing_obj.notes)
    print(f"Total footnotes discovered: {len(all_notes)}")

    # 5. Filter and categorize notes
    notes_with_tables = []
    notes_without_tables = []

    for note in all_notes:
        has_tables = hasattr(note, "tables") and note.tables and len(note.tables) > 0
        has_details = hasattr(note, "details") and note.details and len(note.details) > 0
        
        if has_tables or has_details:
            notes_with_tables.append(note)
        else:
            notes_without_tables.append(note)

    print(f"Notes containing structured data (tables/details): {len(notes_with_tables)}")
    print(f"Notes containing narrative only: {len(notes_without_tables)}")

    # Sample 3 random notes with tables, and 2 random notes without tables
    sampled_with_tables = random.sample(notes_with_tables, min(3, len(notes_with_tables)))
    sampled_without_tables = random.sample(notes_without_tables, min(2, len(notes_without_tables)))

    # 6. Build the Markdown Report
    print("Generating structural Markdown inspection report...")
    output_lines = []
    
    output_lines.append(f"# SEC EDGAR Footnotes Structural Analysis: {ticker}")
    output_lines.append(f"- **Filing Company:** {company.name}")
    output_lines.append(f"- **Form Type:** {latest_filing.form}")
    output_lines.append(f"- **Accession No:** {latest_filing.accession_no}")
    output_lines.append(f"- **Filing Date:** {latest_filing.filing_date}")
    output_lines.append(f"- **Report Generated At:** {datetime.utcnow().isoformat()}Z")
    output_lines.append("\n" + "="*80 + "\n")

    # Part A: Structured Notes (Tables & XBRL Details)
    output_lines.append("## PART A: Footnotes containing Structured Tables (`table = 1`)\n")
    
    for idx, note in enumerate(sampled_with_tables, 1):
        num_tables = len(note.tables) if hasattr(note, "tables") and note.tables else 0
        num_details = len(note.details) if hasattr(note, "details") and note.details else 0
        expands = getattr(note, "expands", [])
        
        output_lines.append(f"### [Selection A-{idx}] Note {note.number}: {note.title}")
        output_lines.append(f"- **Metadata Counts:** `note.tables` = {num_tables} | `note.details` = {num_details}")
        output_lines.append(f"- **XBRL Expands Links:** {expands}\n")
        
        # Narrative preview
        narrative = getattr(note, "text", "") or ""
        preview_len = 1500
        output_lines.append("#### Narrative Content (First 1500 Characters Preview)")
        output_lines.append(f"```text\n{narrative[:preview_len]}\n...\n[Narrative Continued]\n```\n")

        # Process standard tables (note.tables)
        if num_tables > 0:
            output_lines.append("#### Standard Tables (`note.tables`)\n")
            for ti, table in enumerate(note.tables):
                title = getattr(table.render(), "title", "") or f"Table {ti}"
                output_lines.append(f"##### Note {note.number} - Table {ti}: {title}")
                try:
                    df = table.to_dataframe()
                    if df is not None and not df.empty:
                        # Append raw columns list
                        output_lines.append(f"**Extracted Raw Columns:** `{list(df.columns)}`  ")
                        output_lines.append(f"**Shape:** {df.shape[0]} rows × {df.shape[1]} columns\n")
                        # Append raw table representation
                        output_lines.append(df.to_markdown(index=False))
                    else:
                        output_lines.append("*[Empty DataFrame returned]*")
                except Exception as e:
                    output_lines.append(f"*Failed to convert table {ti} to DataFrame: {e}*")
                output_lines.append("\n" + "-"*40 + "\n")

        # Process XBRL structural details (note.details)
        if num_details > 0:
            output_lines.append("#### XBRL Structural Details (`note.details`)\n")
            for di, detail in enumerate(note.details):
                detail_title = f"Detail {di}"
                try:
                    detail_title = str(detail).split("\n")[0] if str(detail) else f"Detail {di}"
                except Exception:
                    pass
                output_lines.append(f"##### Note {note.number} - Detail {di}: {detail_title}")
                try:
                    df = detail.to_dataframe()
                    if df is not None and not df.empty:
                        output_lines.append(f"**Extracted Raw Columns:** `{list(df.columns)}`  ")
                        output_lines.append(f"**Shape:** {df.shape[0]} rows × {df.shape[1]} columns\n")
                        output_lines.append(df.to_markdown(index=False))
                    else:
                        output_lines.append("*[Empty DataFrame returned]*")
                except Exception as e:
                    output_lines.append(f"*Failed to convert detail {di} to DataFrame: {e}*")
                output_lines.append("\n" + "-"*40 + "\n")

        output_lines.append("\n" + "="*80 + "\n")

    # Part B: Narrative-Only Notes
    output_lines.append("## PART B: Footnotes containing Narrative Only (`table = 0`)\n")
    
    for idx, note in enumerate(sampled_without_tables, 1):
        output_lines.append(f"### [Selection B-{idx}] Note {note.number}: {note.title}")
        
        narrative = getattr(note, "text", "") or ""
        output_lines.append("\n#### Narrative Content (First 3000 Characters)")
        output_lines.append(f"```text\n{narrative[:3000]}\n...\n[Narrative truncated for readability]\n```\n")
        output_lines.append("\n" + "="*80 + "\n")

    # 7. Write to File
    output_file = "sec_notes_inspection.md"
    with open(output_file, "w", encoding="utf-8") as fh:
        fh.write("\n".join(output_lines))
    
    print(f"\n[SUCCESS] Inspection Markdown written to: '{os.path.abspath(output_file)}'")

if __name__ == "__main__":
    run_diagnostic()
