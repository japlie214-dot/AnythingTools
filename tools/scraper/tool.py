"""tools/scraper/tool.py

Scout Mode - Hybrid Programmatic Scraper.

Executes Botasaurus browser automation pipeline and generates
Intelligent Manifest with ledger logging and batch management.
"""

import os
import json
import asyncio
import threading
import tempfile
from datetime import datetime, timezone
from typing import Any
from pathlib import Path

import config
from pydantic import BaseModel
from tools.base import BaseTool, ToolResult
from clients.llm import get_llm_client, LLMRequest
from utils.logger import get_dual_logger
from utils.browser_lock import browser_lock
from tools.scraper.task import _run_botasaurus_scraper
from tools.scraper.targets import VALID_TARGET_NAMES
from utils.id_generator import ULID
from database.writer import enqueue_write
from utils.callback_helper import format_callback_message
from utils.artifact_manager import write_artifact, get_artifacts_root

log = get_dual_logger(__name__)


class ScraperInput(BaseModel):
    target_site: str


INPUT_MODEL = ScraperInput


class ScraperTool(BaseTool):
    """Scout Mode: Programmatic execution with Intelligent Manifest generation."""
    
    name = "scraper"
    _last_artifacts = None

    def is_resumable(self, args: dict[str, Any]) -> bool:
        return True

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        """Main entry point for Scout execution."""
        def _fail(status: str, summary: str, next_steps: str) -> str:
            return json.dumps({
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": status,
                "summary": summary,
                "details": {
                    "input_args": args
                },
                "status_overrides": {
                    status: {
                        "description": "Scraper execution aborted early.",
                        "next_steps": next_steps,
                        "rerunnable": True
                    }
                }
            }, ensure_ascii=False)

        cancellation_flag = kwargs.pop("cancellation_flag", None)
        if cancellation_flag is None:
            cancellation_flag = threading.Event()
            
        if cancellation_flag.is_set():
            return _fail("CANCELLING", "Scraper execution canceled.", "Job canceled. No further action needed unless you wish to resubmit.")

        if browser_lock.locked():
            return _fail("FAILED", "System busy: another browser task is running.", "Wait a few minutes and call the `scraper` tool again.")
        
        browser_lock.acquire()
        try:
            return await self._run_internal(args, telemetry, cancellation_flag, **kwargs)
        finally:
            browser_lock.safe_release()

    async def _run_internal(self, args: dict[str, Any], telemetry: Any, cancellation_flag: threading.Event, **kwargs) -> str:
        """Internal implementation with full pipeline."""
        
        job_id = kwargs.get("job_id")
        session_id = str(kwargs.get("session_id") or kwargs.get("chat_id", "0"))
        target_site = args.get("target_site")
        
        batch_id = ULID.generate()
        artifacts_written = []
        
        def _record_artifact(filepath: Path, artifact_type: str, description: str):
            artifacts_written.append({
                "filename": filepath.name,
                "type": artifact_type,
                "description": description
            })

        def _fail_internal(summary: str, next_steps: str) -> str:
            payload = {
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": "FAILED",
                "summary": summary,
                "details": {
                    "input_args": args,
                    "batch_id": batch_id,
                },
                "status_overrides": {
                    "FAILED": {
                        "description": "Scraper validation failed.",
                        "next_steps": next_steps,
                        "rerunnable": False
                    }
                }
            }
            try:
                enqueue_write(
                    "INSERT INTO job_logs (id, job_id, tag, level, status_state, message, payload_json, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ULID.generate(), job_id, "Scraper:Validation:Failed", "WARNING", "FAILED", summary, json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat())
                )
            except Exception:
                pass
            return json.dumps(payload, ensure_ascii=False)

        dry_run = kwargs.get("dry_run", config.TELEMETRY_DRY_RUN)
        if dry_run:
            return _fail_internal("[DRY RUN] Scraper tool execution skipped.", "Disable dry run to execute.")

        if not target_site:
            return _fail_internal("Error: target_site argument is required.", "Provide a valid 'target_site' argument.")

        if target_site not in VALID_TARGET_NAMES:
            valid_list = ", ".join(sorted(VALID_TARGET_NAMES))
            log.dual_log(
                tag="Scraper:Validation",
                message=f"Invalid target_site rejected: {target_site}",
                level="ERROR",
                payload={"received": target_site, "valid_options": list(VALID_TARGET_NAMES)},
            )
            return _fail_internal(f"Error: '{target_site}' is not a valid target site. Valid options: {valid_list}", f"Use one of the valid options: {valid_list}")

        # Scout initialization (legacy ledger removed) — log for auditing
        if job_id and session_id != "0":
            msg = f"The Scout: Starting extraction for {target_site}."
            log.dual_log(tag="Scraper:Init", message=msg, payload={"job_id": job_id, "session_id": session_id, "batch_id": batch_id})

        loop = asyncio.get_running_loop()
        
        def sync_telemetry(msg: str, state: str = "RUNNING"):
            """Synchronous telemetry wrapper."""
            try:
                fut = asyncio.run_coroutine_threadsafe(telemetry(self.status(msg, state)), loop)
                fut.result(timeout=5)
            except Exception:
                pass

        def sync_llm_chat(messages, response_format=None):
            """Synchronous LLM wrapper for curation."""
            async def _call():
                llm = get_llm_client("azure")
                return await llm.complete_chat(LLMRequest(messages=messages, response_format=response_format))
            return asyncio.run_coroutine_threadsafe(_call(), loop).result(timeout=300)

        await telemetry(self.status(f"Launching headful scraper for {target_site}..."))

        try:
            # Run Botasaurus pipeline
            from utils.browser_daemon import get_or_create_driver
            _scrape_driver = get_or_create_driver()
            
            results = await asyncio.to_thread(
                _run_botasaurus_scraper,
                _scrape_driver,
                {
                    "sync_telemetry": sync_telemetry,
                    "sync_llm_chat": sync_llm_chat,
                    "cancellation_flag": cancellation_flag,
                    "target_site": target_site,
                    "job_id": job_id,
                },
            )

            # Persist raw results to artifacts directory
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            try:
                raw_filepath = write_artifact(
                    tool_name="scraper",
                    job_id=job_id or batch_id,
                    artifact_type=f"scraper_output_{ts}",
                    ext="json",
                    content=json.dumps(results, indent=2, ensure_ascii=False)
                )
                _record_artifact(raw_filepath, "json", f"Raw scraper output for {target_site} ({len(results)} URLs)")
            except Exception as e:
                log.dual_log(tag="Scraper:Artifact", message=f"Failed to write raw artifact: {e}", level="WARNING")
                # Fall back to temp directory
                temp_dir = Path("data/temp")
                temp_dir.mkdir(parents=True, exist_ok=True)
                raw_filepath = temp_dir / f"scraper_output_{ts}.json"
                with open(raw_filepath, "w", encoding="utf-8") as fh:
                    json.dump(results, fh, indent=2, ensure_ascii=False)
                _record_artifact(raw_filepath, "json", f"Raw scraper output (temp fallback) for {target_site}")

            # Extract statistics and slim results
            _stats = results.pop("_stats", {})
            slim_list = []
            for _url, _res in results.items():
                if _url.startswith("_"):
                    continue
                if _res.get("status") == "SUCCESS" and _res.get("ulid"):
                    slim_list.append({
                        "ulid": _res["ulid"],
                        "normalized_url": _res.get("normalized_url"),
                        "title": _res.get("title", ""),
                        "conclusion": _res.get("conclusion", ""),
                    })

            # Curate Top N
            await telemetry(self.status("Curating top articles...", "RUNNING"))
            top_10_list = []
            target_curated_count = 10
            
            if slim_list:
                from tools.scraper.curation import Top10Curator
                curator = Top10Curator()
                top_10_list, target_curated_count = curator.curate(slim_list, sync_llm_chat, batch_id=batch_id)

            # Save Top 10 directly to AnythingLLM custom-documents
            try:
                top_10_path = write_artifact(
                    tool_name="scraper",
                    job_id=job_id or batch_id,
                    artifact_type="top10",
                    ext="json",
                    content=json.dumps(top_10_list, indent=2, ensure_ascii=False)
                )
                self._last_artifacts = [str(top_10_path)]
                _record_artifact(top_10_path, "json", f"Curated Top {target_curated_count} articles for {target_site}")
            except Exception as e:
                log.dual_log(tag="Scraper:Artifact", message=f"Failed to write artifact: {e}", level="WARNING")
                self._last_artifacts = []
                top_10_path = raw_filepath

            # 📝 Generate Directory Index Manifest
            manifest_timestamp = datetime.now(timezone.utc).isoformat()
            manifest_lines = [
                f"# Job Manifest: {self.name}",
                f"",
                f"**Tool Name:** {self.name}",
                f"**Timestamp:** {manifest_timestamp}",
                f"**Batch ID:** {batch_id}",
                f"**Input Parameters:** `{{\"target_site\": \"{target_site}\"}}`",
                f"",
                f"## Output Files",
                f"The following artifacts were produced and saved to this batch directory:"
            ]
            for art in artifacts_written:
                manifest_lines.append(f"- **{art['filename']}** ({art['type']}) - {art['description']}")

            manifest_content = "\n".join(manifest_lines)
            
            # Persist manifest as artifact
            try:
                manifest_path = write_artifact(
                    tool_name="scraper",
                    job_id=job_id or batch_id,
                    artifact_type="manifest",
                    ext="md",
                    content=manifest_content
                )
                _record_artifact(manifest_path, "md", "Directory index of job artifacts")
                if getattr(self, "_last_artifacts", None) is not None:
                    self._last_artifacts.append(str(manifest_path))
            except Exception as e:
                log.dual_log(tag="Scraper:Artifact", message=f"Failed to write manifest: {e}", level="WARNING")

            # Save to broadcast_batches
            try:
                enqueue_write(
                    "INSERT INTO broadcast_batches (batch_id, target_site, raw_json_path, curated_json_path, status) VALUES (?, ?, ?, ?, 'PENDING')",
                    (batch_id, target_site, str(raw_filepath), str(top_10_path))
                )
            except Exception:
                pass

            # Construct callback markdown BEFORE finalizing status
            job_ref = job_id or batch_id
            safe_job_id = __import__('re').sub(r"[^A-Za-z0-9_-]", "", job_ref)
            artifact_subdir = f"scraper/{safe_job_id}"
            
            callback_artifacts = []
            for art in artifacts_written:
                callback_artifacts.append({
                    "filename": art["filename"],
                    "type": art["type"],
                    "description": art["description"],
                    "path": f"{artifact_subdir}/{art['filename']}"
                })

            total = len(slim_list)
            top_10_ulids = {item['ulid'] for item in top_10_list}
            remaining = [item for item in slim_list if item['ulid'] not in top_10_ulids]
            next_50 = remaining[:50]

            callback_lines = [
                f"### Curated Content Preview",
                f"#### Top {len(top_10_list)} Articles"
            ]
            for i, item in enumerate(top_10_list, 1):
                callback_lines.append(f"{i}. **{item['title']}**\n   - URL: {item['normalized_url']}\n   - Conclusion: {item['conclusion']}\n   - ULID: `{item['ulid']}`")
            
            if next_50:
                callback_lines.append(f"\n#### Extended Inventory (Next {len(next_50)} Articles)")
                for item in next_50:
                    callback_lines.append(f"- {item['title']} (ULID: `{item['ulid']}`)")
            
            if total > (len(top_10_list) + len(next_50)):
                remaining_count = total - (len(top_10_list) + len(next_50))
                callback_lines.append(
                    f"\n---\n\n"
                    f"**Extended Content:** This batch contains {total} articles total. "
                    f"The Top {len(top_10_list)} articles are shown in detail above, "
                    f"and the next {len(next_50)} article titles are listed as a preview. "
                    f"To explore the remaining {remaining_count} articles, use the `batch_reader` tool "
                    f"with `{{\"batch_id\": \"{batch_id}\", \"query\": \"<your semantic search query>\"}}`. "
                    f"The `batch_reader` performs hybrid semantic search (combining vector similarity "
                    f"and full-text indexing) to find relevant articles based on meaning, providing summaries and conclusions for deeper insight."
                )
            callback_summary_markdown = "\n".join(callback_lines)

            # Update job status BEFORE creating payload
            await telemetry(self.status("Scraper and curation finished.", "SUCCESS"))
            
            job_final_status = _stats.get("job_final_status", "COMPLETED")
            if len(top_10_list) < target_curated_count and len(top_10_list) > 0:
                job_final_status = "PARTIAL"
            elif len(top_10_list) == 0 and total > 0:
                job_final_status = "PARTIAL"
            
            payload = {
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": job_final_status,
                "summary": callback_summary_markdown,
                "details": {
                    "input_args": args,
                    "batch_id": batch_id,
                    "target_site": target_site,
                    "total_articles": total,
                    "target_curated_count": target_curated_count,
                    "actual_curated_count": len(top_10_list),
                    "inventory_count": len(next_50),
                    "artifacts_directory": artifact_subdir
                },
                "artifacts": callback_artifacts,
                "status_overrides": {
                    "PARTIAL": {
                        "description": f"Scraper completed but curation selected only {len(top_10_list)}/{target_curated_count} articles. Some URLs may have failed.",
                        "next_steps": f"Call the `scraper` tool again using exactly: {{\"target_site\": \"{target_site}\"}}. The system will skip completed URLs and retry failed ones.",
                        "rerunnable": True
                    },
                    "COMPLETED": {
                        "description": "Scrape successful and batch generated with full curation.",
                        "next_steps": f"To query this batch's inventory, call `batch_reader` with {{\"batch_id\": \"{batch_id}\", \"query\": \"<your search>\"}}. To publish the Top 10 to Telegram, call `publisher` with {{\"batch_id\": \"{batch_id}\"}}.",
                        "rerunnable": False
                    },
                    "FAILED": {
                        "description": "Scraping failed entirely. Target site might be blocking access.",
                        "next_steps": "Check the error details. Call the `scraper` tool again but use a different site, e.g., {\"target_site\": \"Bloomberg\"}.",
                        "rerunnable": True
                    }
                }
            }

            log.dual_log(
                tag="Scraper:Job:Status",
                message=f"Job finalization status: {job_final_status}",
                payload={"final_status": job_final_status}
            )
            
            callback_json = json.dumps(payload, ensure_ascii=False)
            try:
                enqueue_write(
                    "INSERT INTO job_logs (id, job_id, tag, level, status_state, message, payload_json, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ULID.generate(), job_id, "Scraper:Callback:Payload", "INFO", job_final_status,
                     f"Callback payload prepared with {len(callback_artifacts)} artifacts", callback_json,
                     datetime.now(timezone.utc).isoformat())
                )
            except Exception:
                pass

            return callback_json

        except Exception as exc:
            if str(exc).startswith("PAUSED_FOR_HITL:"):
                raise
            
            log.dual_log(
                tag="Scraper:Error",
                message=f"Scraper execution crashed: {exc}",
                level="ERROR",
                payload={"batch_id": batch_id if 'batch_id' in locals() else None, "job_id": job_id},
                exc_info=exc
            )
            
            await telemetry(self.status("Scraper failed.", "ERROR"))
            
            err_payload = {
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": "FAILED",
                "summary": f"Scraper execution crashed: {exc}",
                "details": {
                    "input_args": args,
                    "batch_id": batch_id if 'batch_id' in locals() else None,
                    "error_type": type(exc).__name__,
                },
                "status_overrides": {
                    "FAILED": {
                        "description": "Scraping crashed fatally.",
                        "next_steps": "Review system logs for the full Traceback. This usually indicates a code error or file system issue.",
                        "rerunnable": False
                    }
                }
            }
            try:
                enqueue_write(
                    "INSERT INTO job_logs (id, job_id, tag, level, status_state, message, payload_json, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ULID.generate(), job_id, "Scraper:Error:Payload", "ERROR", "FAILED",
                     f"Scraper crashed: {exc}", json.dumps(err_payload, ensure_ascii=False),
                     datetime.now(timezone.utc).isoformat())
                )
            except Exception:
                pass

            return json.dumps(err_payload, ensure_ascii=False)

    async def execute(self, args, telemetry, **kwargs) -> ToolResult:
        """Override to include artifacts in result."""
        base_res = await super().execute(args, telemetry, **kwargs)
        artifacts = getattr(self, "_last_artifacts", None)
        return ToolResult(
            output=base_res.output,
            success=base_res.success,
            attachment_paths=artifacts,
            event_id=base_res.event_id,
            diagnosis=base_res.diagnosis,
        )
