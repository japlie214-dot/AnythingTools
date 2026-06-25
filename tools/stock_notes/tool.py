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

    @activity("Build Discover Payload")
    def _build_discover_payload(self, ticker: str, filings: list) -> dict:
        """Build JSON payload for discover command."""
        return {
            "ticker": ticker,
            "count": len(filings),
            "filings": filings,
            "next_step_hint": 'Use "note" command with instructions {"accession_no": "<accession_no>"}.',
        }

    @activity("Build Note Payload")
    def _build_note_payload(self, accession_no: str, filing_row: tuple, note_number: int | None, conn, job_id: str) -> dict:
        """Build JSON payload for note command."""
        f_ticker, f_form, f_company, f_period, f_quarter, f_year, f_fye = filing_row
        from .detail_manager import build_concept_catalog, get_date_range_for_filing

        if note_number is None:
            notes = conn.execute(
                "SELECT note_number, title, short_name, table_count, details_count, quarterly_status "
                "FROM sn_notes WHERE accession_no=? ORDER BY note_number",
                (accession_no,),
            ).fetchall()
            if not notes:
                return {"error": f"No notes found in filing {accession_no}."}

            notes_list = []
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
                notes_list.append({
                    "number": n[0], "title": n[1], "short_name": n[2],
                    "tables": n[3], "details": n[4], "status": n[5], "concepts_preview": concept_str
                })
            return {
                "ticker": f_ticker, "company": f_company, "accession": accession_no,
                "form": f_form, "period": f_period, "quarter": f_quarter, "year": f_year,
                "notes": notes_list
            }

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
                job_id=job_id,
                next_steps="Check available notes.",
            )

        (n_num, n_title, n_short, n_narrative, n_expands, n_expands_stmts,
         n_tbl_count, n_dt_count, n_q_status) = note_row
        dts = conn.execute(
            "SELECT detail_table_name, source_title, role_or_type, available_concepts "
            "FROM sn_detail_registry WHERE source_accession_no=? AND source_note_number=?",
            (accession_no, note_number),
        ).fetchall()

        narrative_path = None
        if n_narrative:
            try:
                art_path = write_artifact(self.name, job_id, "narrative", "md", n_narrative)
                narrative_path = str(art_path)
                self._last_artifacts = [narrative_path]
            except Exception as e:
                log.dual_log(tag="StockNotes:NarrativeArtifact:Failed", message=f"Failed to write narrative artifact: {e}", level="WARNING", payload={"error": str(e)})

        catalog = None
        if dts:
            catalog = build_concept_catalog(f_ticker, accession_no, note_number)

        return {
            "note": {
                "number": n_num, "title": n_title, "short_name": n_short,
                "narrative_path": narrative_path, "expands": n_expands,
                "tables": n_tbl_count, "details": n_dt_count, "status": n_q_status
            },
            "company": f_company, "ticker": f_ticker, "accession": accession_no,
            "form": f_form, "quarter": f_quarter, "year": f_year,
            "concept_catalog": catalog,
            "detail_tables": [d[0] for d in dts]
        }

    @activity("Query Concept Details")
    def _query_concept_details(self, ticker: str, concept: str, start_date: str | None, end_date: str | None) -> list:
        """Query tidy table for concept details. Returns records or raises ValueError."""
        from .detail_manager import query_tidy_table
        try:
            return query_tidy_table(ticker, concept, start_date, end_date)
        except ValueError as e:
            raise ToolExecutionError(
                f"Invalid date format in details query: {e}",
                tool_name=self.name,
                next_steps="Use YYYY-MM format for start_date and end_date (e.g., '2023-01').",
            ) from e

    @activity("Build Details Payload")
    def _build_details_payload(self, concept: str, ticker: str, records: list, start_date: str | None, end_date: str | None, job_id: str) -> dict:
        """Build JSON payload for details command."""
        from .detail_manager import format_as_markdown_table
        md_table = format_as_markdown_table(records, f"Time Series: {concept}")
        try:
            art_path = write_artifact(self.name, job_id, "detail_table", "md", md_table)
            detail_table_path = str(art_path)
            self._last_artifacts = [detail_table_path]
        except Exception as e:
            log.dual_log(tag="StockNotes:DetailArtifact:Failed", message=f"Failed to write detail artifact: {e}", level="WARNING", payload={"error": str(e)})
            detail_table_path = None

        return {
            "concept": concept,
            "ticker": ticker,
            "records": records,
            "record_count": len(records),
            "date_range": {"start": start_date, "end": end_date},
            "detail_table_path": detail_table_path,
        }

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
            # Step 3: Build payload.
            return json.dumps(self._build_discover_payload(ticker, filings), ensure_ascii=False, default=str)

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

            # Step 3: Build note payload.
            return json.dumps(self._build_note_payload(accession_no, filing_row, note_number, conn, job_id), ensure_ascii=False, default=str)

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
            # Step 3: Build details payload.
            return json.dumps(self._build_details_payload(concept, ticker, records, start_date, end_date, job_id), ensure_ascii=False, default=str)
