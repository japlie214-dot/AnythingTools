# tools/stock_notes/tool.py
"""Stock Notes Tool - SEC EDGAR filing note extraction and concept querying.

Activity-Driven Observability:
  Decomposed into named activities per command path.
  See utils/observability/activity_decorator.py.
"""

import json
from typing import Any
from tools.base import BaseTool, ToolExecutionError, ToolValidationError
from .models import StockNotesInput
from utils.logger import get_dual_logger
from utils.artifact_manager import write_artifact
from utils.context_helpers import to_thread_with_context
from database.writer import wait_for_writes
from utils.observability.activity_decorator import activity

log = get_dual_logger(__name__)

class StockNotesTool(BaseTool):
    name = "stock_notes"
    INPUT_MODEL = StockNotesInput

    def is_resumable(self, args: dict[str, Any]) -> bool:
        return True

    # --- Activity-decomposed sub-methods ---

    @activity("Validate StockNotes Input")
    def _validate_stocknotes_input(self, args: dict, job_id: str) -> tuple:
        """Parse and validate command, ticker, forms, accession_no, etc. Raises on invalid command."""
        cmd = args.get("command", "").lower().strip()
        raw_inst = args.get("instructions", {})
        if isinstance(raw_inst, str):
            try:
                instructions = json.loads(raw_inst)
            except Exception:
                raise ToolExecutionError(
                    "Invalid instructions payload",
                    tool_name=self.name,
                    job_id=job_id,
                    next_steps="The instructions parameter must be a valid JSON object.",
                )
        else:
            instructions = raw_inst

        ticker = (instructions.get("ticker") or "").upper().strip()
        forms = instructions.get("forms") or "10-K,10-Q"
        accession_no = (instructions.get("accession_no") or "").strip()
        note_number = instructions.get("note_number")
        concept = (instructions.get("concept") or "").strip()
        start_date = (instructions.get("start_date") or "").strip() or None
        end_date = (instructions.get("end_date") or "").strip() or None

        if cmd not in ("discover", "note", "details"):
            raise ToolExecutionError(
                "Invalid command",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Use discover, note, or details.",
            )

        return cmd, instructions, ticker, forms, accession_no, note_number, concept, start_date, end_date

    @activity("Discover Filings")
    async def _discover_filings(self, ticker: str, forms: str) -> list:
        """Call EDGAR to discover available filings. Returns list of filing dicts."""
        from .extractor import discover_filings
        form_types = [f.strip() for f in forms.split(",") if f.strip()]
        return await to_thread_with_context(discover_filings, ticker, form_types=form_types, limit=40)

    @activity("Extract and Persist Filing")
    async def _extract_filing(self, accession_no: str, ticker: str, job_id: str, force_refresh: bool) -> None:
        """Extract filing from EDGAR and persist to DB. Raises on failure."""
        from .extractor import extract_and_persist_filing
        try:
            await to_thread_with_context(
                extract_and_persist_filing, accession_no,
                ticker=ticker, job_id=job_id, force_refresh=force_refresh,
            )
            await wait_for_writes(timeout=30.0)
        except Exception as e:
            raise ToolExecutionError(
                f"Extraction failed: {e}",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Ensure valid accession_no and EDGAR connectivity.",
            ) from e

    @activity("Build Discover Markdown")
    def _build_discover_markdown(self, ticker: str, filings: list) -> str:
        """Build the discover results markdown table."""
        lines = [f"# Filings for {ticker} ({len(filings)} found, newest first)\n"]
        lines.append("| # | Form | Filing Date | Period | Quarter | Accession No |")
        lines.append("|---|------|-------------|--------|---------|--------------|")
        for i, f in enumerate(filings):
            quarter_str = f"Q{f['quarter']} FY{f['year']}" if f.get("quarter") else "N/A"
            lines.append(
                f"| {i+1} | {f['form']} | {f['filing_date']} | "
                f"{f.get('period_of_report', 'N/A')} | {quarter_str} | {f['accession_no']} |"
            )
        lines.append(f"\nUse `note` command with instructions `{{\"accession_no\": \"<accession_no>\"}}` to list notes.")
        return "\n".join(lines)

    @activity("Build Note Markdown")
    def _build_note_markdown(self, accession_no: str, filing_row: tuple, note_number: int | None, conn) -> str:
        """Build the note listing or detail markdown. Returns markdown string."""
        f_ticker, f_form, f_company, f_period, f_quarter, f_year, f_fye = filing_row
        from .detail_manager import build_concept_catalog, get_date_range_for_filing

        if note_number is None:
            notes = conn.execute(
                "SELECT note_number, title, short_name, table_count, details_count, quarterly_status "
                "FROM sn_notes WHERE accession_no=? ORDER BY note_number",
                (accession_no,),
            ).fetchall()
            if not notes:
                return f"No notes found in filing {accession_no}."

            lines = [
                f"# Notes in {f_form} Filing: {f_company} ({f_ticker})",
                f"**Accession:** {accession_no} | **Period:** {f_period} | Q{f_quarter} FY{f_year}\n",
                "| Note# | Title | Tables | Details | Q Status | Concepts Preview |",
                "|-------|-------|--------|---------|----------|------------------|",
            ]
            for n in notes:
                concept_str = ""
                if n[4] > 0:
                    concepts = conn.execute(
                        "SELECT DISTINCT concept FROM sn_note_details "
                        "WHERE accession_no = ? AND note_number = ? "
                        "AND abstract = 'False' AND value != '' LIMIT 5",
                        (accession_no, n[0]),
                    ).fetchall()
                    if concepts:
                        concept_str = ", ".join(c[0].replace("us-gaap:", "") for c in concepts)
                lines.append(f"| {n[0]} | {n[1]} | {n[3]} | {n[4]} | {n[5]} | {concept_str} |")
            return "\n".join(lines)

        # Specific note detail
        note_row = conn.execute(
            "SELECT note_number, title, short_name, narrative_text, expands, expands_statements, "
            "table_count, details_count, quarterly_status "
            "FROM sn_notes WHERE accession_no=? AND note_number=?",
            (accession_no, note_number),
        ).fetchone()
        if not note_row:
            raise ToolExecutionError(
                f"Note {note_number} not found",
                tool_name=self.name,
                job_id=self.job_id if hasattr(self, 'job_id') else None,
                next_steps="Check available notes.",
            )

        (n_num, n_title, n_short, n_narrative, n_expands, n_expands_stmts,
         n_tbl_count, n_dt_count, n_q_status) = note_row
        dts = conn.execute(
            "SELECT detail_table_name, source_title, role_or_type, available_concepts "
            "FROM sn_detail_registry WHERE source_accession_no=? AND source_note_number=?",
            (accession_no, note_number),
        ).fetchall()

        lines = [
            f"# Note {n_num}: {n_title}",
            f"**Company:** {f_company} ({f_ticker})",
            f"**Accession:** {accession_no} | **Form:** {f_form}",
            f"**Quarter:** Q{f_quarter} FY{f_year} | **Status:** {n_q_status}",
            f"**Tables:** {n_tbl_count} | **Details:** {n_dt_count} | **Detail Tables:** {len(dts)}",
        ]
        if n_expands:
            try:
                expands = json.loads(n_expands)
                if expands:
                    lines.append(f"**Expands:** {', '.join(str(e) for e in expands[:5])}")
            except Exception:
                pass
        if n_narrative:
            display_narrative = n_narrative if len(n_narrative) <= 200000 else n_narrative[:200000] + "\n\n...[Truncated. See Artifact for full text]."
            lines.append(f"\n## Narrative\n\n{display_narrative}")
        else:
            lines.append("\n*No narrative content available.*")

        if dts:
            catalog = build_concept_catalog(f_ticker, accession_no, note_number)
            start_date, end_date = get_date_range_for_filing(accession_no)
            if catalog:
                lines.append(f"\n## Concept Catalog ({len(catalog)} queryable concepts)\n")
                lines.append("| # | Concept | Label | Axis | Member | Periods | Range |")
                lines.append("|---|---------|-------|------|--------|---------|-------|")
                for ci, entry in enumerate(catalog, 1):
                    axis_short = entry.get("dimension_axis", "").replace("us-gaap:", "").replace("Axis", "") if entry.get("dimension_axis") else "\u2014"
                    member = entry.get("dimension_member_label") or "\u2014"
                    er = entry.get("earliest_period", "")[:7] if entry.get("earliest_period") else "?"
                    lr = entry.get("latest_period", "")[:7] if entry.get("latest_period") else "?"
                    pc = entry.get("period_count", "?")
                    lines.append(f"| {ci} | `{entry['concept']}` | {entry['label']} | {axis_short} | {member} | {pc} | {er} \u2192 {lr} |")
            else:
                lines.append("\n*No queryable concepts found in this note.*")
        else:
            lines.append("\n*No detail concepts found for this note.*")

        return "\n".join(lines)

    @activity("Query Concept Details")
    def _query_concept_details(self, ticker: str, concept: str, start_date: str | None, end_date: str | None) -> list:
        """Query tidy table for concept details. Returns records or raises ValueError."""
        from .detail_manager import query_tidy_table
        return query_tidy_table(ticker, concept, start_date, end_date)

    @activity("Build Details Markdown")
    def _build_details_markdown(self, concept: str, ticker: str, records: list, start_date: str | None, end_date: str | None, job_id: str) -> str:
        """Build the concept details markdown table."""
        from .detail_manager import format_as_markdown_table
        md_table = format_as_markdown_table(records, f"Time Series: {concept}")
        art_path = write_artifact(self.name, job_id, "detail_table", "md", md_table)
        lines = [f"# Concept Details: {concept} ({ticker})", f"**Records:** {len(records)}"]
        if start_date and end_date:
            lines.append(f"**Date range:** {start_date} to {end_date}")
        lines.extend(["", md_table])
        return "\n".join(lines)

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        job_id = kwargs.get("job_id", "")

        # Step 1: Validate input (raises on invalid command).
        cmd, instructions, ticker, forms, accession_no, note_number, concept, start_date, end_date = \
            self._validate_stocknotes_input(args, job_id)

        if cmd == "discover":
            if not ticker:
                raise ToolExecutionError(
                    "Missing ticker",
                    tool_name=self.name,
                    job_id=job_id,
                    next_steps="Provide a ticker symbol in the instructions payload.",
                )
            # Step 2: Discover filings.
            filings = await self._discover_filings(ticker, forms)
            if not filings:
                raise ToolExecutionError(
                    f"No filings found for {ticker}",
                    tool_name=self.name,
                    job_id=job_id,
                    next_steps="Verify the ticker.",
                )
            # Step 3: Build markdown.
            return self._build_discover_markdown(ticker, filings)

        elif cmd == "note":
            from database.connection import DatabaseManager
            if not accession_no:
                raise ToolExecutionError(
                    "Missing accession_no",
                    tool_name=self.name,
                    job_id=job_id,
                    next_steps="Provide an accession number in the instructions payload.",
                )
            force_refresh = instructions.get("force_refresh", False)
            conn = DatabaseManager.get_read_connection()
            exists_locally = conn.execute(
                "SELECT 1 FROM sn_filings WHERE accession_no=?", (accession_no,)
            ).fetchone()

            if force_refresh or not exists_locally:
                # Step 2: Extract filing.
                await self._extract_filing(accession_no, ticker, job_id, force_refresh)
                conn = DatabaseManager.get_read_connection()

            # Auto-hydrate for specific note if missing.
            if note_number is not None:
                note_exists = conn.execute(
                    "SELECT 1 FROM sn_notes WHERE accession_no=? AND note_number=? LIMIT 1",
                    (accession_no, note_number),
                ).fetchone()
                if not note_exists and not force_refresh:
                    try:
                        await self._extract_filing(accession_no, ticker, job_id, force_refresh=False)
                        conn = DatabaseManager.get_read_connection()
                    except Exception as e:
                        log.dual_log(
                            tag="StockNotes:AutoHydrate", level="WARNING",
                            message=f"Auto-hydration failed for note {note_number}: {e}",
                            payload={"accession_no": accession_no, "note_number": note_number},
                        )

            filing_row = conn.execute(
                "SELECT ticker, form, company_name, period_of_report, quarter, year, fiscal_year_end_month "
                "FROM sn_filings WHERE accession_no=?",
                (accession_no,),
            ).fetchone()
            if not filing_row:
                raise ToolExecutionError(
                    f"Filing {accession_no} not found after extraction.",
                    tool_name=self.name,
                    job_id=job_id,
                    next_steps="Try the discover command first.",
                )

            # Step 3: Build note markdown.
            return self._build_note_markdown(accession_no, filing_row, note_number, conn)

        elif cmd == "details":
            if not ticker or not concept:
                raise ToolExecutionError(
                    "Missing ticker or concept.",
                    tool_name=self.name,
                    job_id=job_id,
                    next_steps="Provide both 'ticker' and 'concept' in the instructions payload.",
                )
            # Step 2: Query concept details.
            records = self._query_concept_details(ticker, concept, start_date, end_date)
            if not records:
                raise ToolExecutionError(
                    f"No records found for concept '{concept}' on ticker {ticker}",
                    tool_name=self.name,
                    job_id=job_id,
                    next_steps="Adjust date range or extract notes first.",
                )
            # Step 3: Build details markdown.
            return self._build_details_markdown(concept, ticker, records, start_date, end_date, job_id)
