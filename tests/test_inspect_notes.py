# tests/test_inspect_notes.py
"""Real-workflow validation of SEC EDGAR footnote extraction.

This is the pytest-collectable successor to the legacy tests/inspect_notes.py
standalone script. It exercises the real `edgartools` API (Company,
set_identity, filings, filing.obj().notes, note.tables, note.details,
table.to_dataframe, detail.to_dataframe) against a live SEC EDGAR 10-K
filing, with the database layer disabled (per the autouse
_disable_db_integration fixture in conftest.py).

Design:
- Marker `db_off` (registered in pytest.ini) makes the DB-bypass explicit.
- Marker `network` allows CI to skip when offline: `pytest -m "not network"`.
- Writes the markdown report to tmp_path (NOT CWD) for test isolation.
- Uses AAPL as a stable, high-availability ticker with a guaranteed 10-K history.

References:
- edgartools API: https://sec-api.github.io/edgartools/
- pytest markers: https://docs.pytest.org/en/stable/how-to/mark.html
- pytest tmp_path: https://docs.pytest.org/en/stable/how-to/tmp_path.html
- SEC EDGAR fair access: https://www.sec.gov/os/accessing-edgar-data
"""
from __future__ import annotations

import os
import random
from datetime import datetime
from pathlib import Path

import pytest

# Markers — both must be registered in pytest.ini.
# pytestmark applies to every test in the module.
# Per https://docs.pytest.org/en/stable/how-to/mark.html
pytestmark = [pytest.mark.db_off, pytest.mark.network]


def _require_edgar_identity() -> str:
    """Return the EDGAR identity, skipping the test if unset.

    SEC EDGAR requires a User-Agent with a real email. Per the edgartools
    docs (https://sec-api.github.io/edgartools/) and SEC's fair-access
    policy (https://www.sec.gov/os/accessing-edgar-data), the identity
    must identify the caller.
    """
    identity = os.getenv("EDGAR_IDENTITY", "")
    if not identity or "@" not in identity:
        pytest.skip(
            "EDGAR_IDENTITY env var not set or invalid; "
            "skipping live SEC EDGAR test"
        )
    return identity


def _require_edgartools():
    """Import edgartools, skipping if not installed.

    Per https://docs.pytest.org/en/stable/how-to/skipping.html
    "skipping tests that depend on an external resource which is not
    available at the moment."
    """
    try:
        from edgar import Company, set_identity  # noqa: F401
        return Company, set_identity
    except ImportError:
        pytest.skip("edgartools not installed; run `pip install edgartools`")


def test_retrieve_latest_10k_and_inspect_notes(tmp_path: Path):
    """End-to-end: retrieve AAPL's latest 10-K, parse footnotes, validate
    the structured-data API surface that the stock_notes tool depends on.

    This is a CONTRACT test: it verifies that edgartools still exposes
    `filing.obj().notes`, `note.tables`, `note.details`, `table.to_dataframe()`,
    and `detail.to_dataframe()` — the exact API surface used by
    `tools/stock_notes/extractor.py` and `tools/stock_notes/tidy_transform.py`.
    If edgartools changes any of these, this test fails BEFORE the stock_notes
    tool breaks in production.
    """
    Company, set_identity = _require_edgartools()
    identity = _require_edgar_identity()
    set_identity(identity)

    ticker = "AAPL"
    company = Company(ticker)

    # 1. Retrieve the latest 10-K (amendments excluded — we want the original).
    filings = company.get_filings(form="10-K", amendments=False)
    assert filings, f"No 10-K filings found for {ticker}"
    latest_filing = filings[0]
    assert latest_filing.form == "10-K"
    assert latest_filing.accession_no, "Filing missing accession_no"
    assert latest_filing.filing_date, "Filing missing filing_date"

    # 2. Parse the filing object and verify the notes collection exists.
    #    This is the API surface that tools/stock_notes/extractor.py depends on.
    filing_obj = latest_filing.obj()
    assert hasattr(filing_obj, "notes"), (
        "edgartools Filing.obj() no longer exposes .notes — "
        "tools/stock_notes/extractor.py will break."
    )
    assert filing_obj.notes, "Filing has no parseable footnotes collection"

    all_notes = list(filing_obj.notes)
    assert len(all_notes) > 0, "Filing returned an empty notes iterator"

    # 3. Categorize notes by structural richness (tables/details vs narrative-only).
    notes_with_tables = [
        n for n in all_notes
        if getattr(n, "tables", None) or getattr(n, "details", None)
    ]
    notes_without_tables = [n for n in all_notes if n not in notes_with_tables]

    # A real 10-K should have at least one note with structured data.
    # If this fails, either edgartools changed its API OR the filing is
    # degenerate — both warrant investigation.
    assert len(notes_with_tables) > 0, (
        "No notes with structured tables/details found in latest 10-K — "
        "edgartools API may have changed."
    )

    # 4. Sample notes (deterministic seed for reproducibility within a filing).
    random.seed(42)
    sampled_with = random.sample(notes_with_tables, min(3, len(notes_with_tables)))
    sampled_without = random.sample(notes_without_tables, min(2, len(notes_without_tables)))

    # 5. Validate the table.to_dataframe() and detail.to_dataframe() contract.
    #    These methods are the foundation of tools/stock_notes/tidy_transform.py.
    for note in sampled_with:
        assert hasattr(note, "number"), "Note missing .number attribute"
        assert hasattr(note, "title"), "Note missing .title attribute"

        if getattr(note, "tables", None):
            for table in note.tables:
                # table.to_dataframe() must return a DataFrame or None.
                # An empty DataFrame is acceptable; an exception is not.
                try:
                    df = table.to_dataframe()
                except Exception as e:
                    pytest.fail(
                        f"table.to_dataframe() raised {type(e).__name__}: {e} — "
                        f"tools/stock_notes/tidy_transform.py will break."
                    )
                if df is not None:
                    import pandas as pd
                    assert isinstance(df, pd.DataFrame), (
                        f"Expected DataFrame, got {type(df)}"
                    )

        if getattr(note, "details", None):
            for detail in note.details:
                try:
                    df = detail.to_dataframe()
                except Exception as e:
                    pytest.fail(
                        f"detail.to_dataframe() raised {type(e).__name__}: {e}"
                    )
                if df is not None:
                    import pandas as pd
                    assert isinstance(df, pd.DataFrame), (
                        f"Expected DataFrame, got {type(df)}"
                    )

    # 6. Generate the markdown report (replaces the legacy script's output).
    #    Write to tmp_path for test isolation (NOT CWD).
    #    Per https://docs.pytest.org/en/stable/how-to/tmp_path.html
    report_path = tmp_path / "sec_notes_inspection.md"
    _write_markdown_report(
        report_path,
        company_name=company.name,
        form=latest_filing.form,
        accession_no=latest_filing.accession_no,
        filing_date=latest_filing.filing_date,
        total_notes=len(all_notes),
        notes_with_tables=len(notes_with_tables),
        notes_without_tables=len(notes_without_tables),
        sampled_with=sampled_with,
        sampled_without=sampled_without,
    )

    assert report_path.exists(), "Markdown report was not written"
    assert report_path.stat().st_size > 0, "Markdown report is empty"


def _write_markdown_report(
    path: Path,
    *,
    company_name: str,
    form: str,
    accession_no: str,
    filing_date: str,
    total_notes: int,
    notes_with_tables: int,
    notes_without_tables: int,
    sampled_with: list,
    sampled_without: list,
) -> None:
    """Write a structural inspection report to `path`.

    This is a pytest-friendly extraction of the legacy
    tests/inspect_notes.py:run_diagnostic markdown writer. The output
    format is preserved for backward compatibility with any downstream
    tooling that consumed the old `sec_notes_inspection.md`.
    """
    lines = [
        "# SEC EDGAR Footnotes Structural Analysis: AAPL",
        f"- **Filing Company:** {company_name}",
        f"- **Form Type:** {form}",
        f"- **Accession No:** {accession_no}",
        f"- **Filing Date:** {filing_date}",
        f"- **Report Generated At:** {datetime.utcnow().isoformat()}Z",
        f"- **Total Notes:** {total_notes}",
        f"- **Notes with structured data:** {notes_with_tables}",
        f"- **Notes narrative-only:** {notes_without_tables}",
        "",
        "=" * 80,
        "",
        "## PART A: Footnotes containing Structured Tables",
        "",
    ]
    for idx, note in enumerate(sampled_with, 1):
        num_tables = len(note.tables) if getattr(note, "tables", None) else 0
        num_details = len(note.details) if getattr(note, "details", None) else 0
        lines.append(f"### [A-{idx}] Note {note.number}: {note.title}")
        lines.append(f"- tables: {num_tables}, details: {num_details}")
        narrative = getattr(note, "text", "") or ""
        lines.append(f"- narrative preview ({len(narrative)} chars): {narrative[:300]!r}")
        lines.append("")
    lines.append("=" * 80)
    lines.append("")
    lines.append("## PART B: Footnotes containing Narrative Only")
    lines.append("")
    for idx, note in enumerate(sampled_without, 1):
        lines.append(f"### [B-{idx}] Note {note.number}: {note.title}")
        narrative = getattr(note, "text", "") or ""
        lines.append(f"- narrative preview ({len(narrative)} chars): {narrative[:300]!r}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
