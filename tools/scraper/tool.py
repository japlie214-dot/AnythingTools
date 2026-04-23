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
        def _fail_internal(summary: str, next_steps: str) -> str:
            return json.dumps({
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": "FAILED",
                "summary": summary,
                "status_overrides": {
                    "FAILED": {
                        "description": "Scraper validation failed.",
                        "next_steps": next_steps,
                        "rerunnable": False
                    }
                }
            }, ensure_ascii=False)

        dry_run = kwargs.get("dry_run", config.TELEMETRY_DRY_RUN)
        if dry_run:
            return _fail_internal("[DRY RUN] Scraper tool execution skipped.", "Disable dry run to execute.")

        job_id = kwargs.get("job_id")
        session_id = str(kwargs.get("session_id") or kwargs.get("chat_id", "0"))
        target_site = args.get("target_site")
        
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
            log.dual_log(tag="Scraper:Init", message=msg, payload={"job_id": job_id, "session_id": session_id})

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

            # Persist raw results to hidden data/temp
            temp_dir = Path("data/temp")
            temp_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            raw_filepath = temp_dir / f"scraper_output_{ts}.json"
            with open(raw_filepath, "w", encoding="utf-8") as fh:
                json.dump(results, fh, indent=2, ensure_ascii=False)

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

            # Curate Top 10
            await telemetry(self.status("Curating Top 10 articles...", "RUNNING"))
            batch_id = ULID.generate()
            top_10_list = []
            
            if slim_list:
                curation_prompt = (
                    "You are a financial intelligence curator.\nReturn ONLY a JSON object with key 'top_10' which is an array of ulids.\n" + json.dumps(slim_list)
                )
                try:
                    llm = get_llm_client("azure")
                    curate_res = await llm.complete_chat(LLMRequest(
                        messages=[{"role": "user", "content": curation_prompt}], 
                        response_format={"type": "json_object"}
                    ))
                    top_10_ulids_res = json.loads(curate_res.content).get("top_10", [])[:10]
                    slim_index = {item["ulid"]: item for item in slim_list}
                    top_10_list = [slim_index[u] for u in top_10_ulids_res if u in slim_index]
                except Exception:
                    top_10_list = slim_list[:10]

            # Save Top 10 directly to AnythingLLM custom-documents
            try:
                top_10_path = write_artifact(
                    tool_name="scraper",
                    job_id=batch_id,
                    artifact_type="top10",
                    ext="json",
                    content=json.dumps(top_10_list, indent=2, ensure_ascii=False)
                )
                self._last_artifacts = [str(top_10_path)]
            except Exception as e:
                log.dual_log(tag="Scraper:Artifact", message=f"Failed to write artifact: {e}", level="WARNING")
                self._last_artifacts = []
                top_10_path = raw_filepath

            # 📝 Generate Intelligent Manifest
            manifest_lines = [
                f"### Scout Intelligence Briefing",
                f"**Target Site:** {target_site}",
                f"**Batch ID:** {batch_id}\n",
                "#### Top 10 Articles"
            ]
            top_10_ulids = set()
            
            for i, item in enumerate(top_10_list, 1):
                manifest_lines.append(
                    f"{i}. **{item['title']}**\n"
                    f"   *URL:* {item['normalized_url']}\n"
                    f"   *Conclusion:* {item['conclusion']}\n"
                    f"   *ULID:* {item['ulid']}\n"
                )
                top_10_ulids.add(item['ulid'])

            # Next 50
            remaining = [item for item in slim_list if item['ulid'] not in top_10_ulids]
            next_50 = remaining[:50]
            
            if next_50:
                manifest_lines.append(f"#### Extended Inventory (Next {len(next_50)})")
                for item in next_50:
                    manifest_lines.append(f"- {item['title']} (ULID: {item['ulid']})")

            # Batch scaling notice
            total = len(slim_list)
            if total > 60:
                manifest_lines.append(
                    f"\n⚠️ NOTICE: This batch contains {total} articles. "
                    f"Only the Top 10 (detailed) and the next 50 (titles) are listed here. "
                    f"To retrieve data from the remaining {total - 60} articles, use the `library:vector_search` tool. "
                    f"Provide the `batch_id: {batch_id}` and a specific `query: string` "
                    f"(e.g., 'semiconductor supply chain constraints') to pull relevant article segments into your active context."
                )

            # Intelligent Manifest generated — persisted via broadcast_batches; log summary
            if job_id and session_id != "0":
                manifest_content = "\n".join(manifest_lines)
                log.dual_log(tag="Scraper:Manifest", message="Intelligent manifest generated.", payload={"job_id": job_id, "manifest": manifest_content})

            # Save to broadcast_batches (raw_json_path points to data/temp)
            try:
                enqueue_write(
                    "INSERT INTO broadcast_batches (batch_id, target_site, raw_json_path, curated_json_path, status) VALUES (?, ?, ?, ?, 'PENDING')",
                    (batch_id, target_site, str(raw_filepath), str(top_10_path))
                )
            except Exception:
                pass

            await telemetry(self.status("Scraper and curation finished.", "SUCCESS"))
            
            job_final_status = _stats.get("job_final_status", "COMPLETED")
            
            payload = {
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": job_final_status,
                "summary": f"Scraped **{total}** articles from **{target_site}**. Curated **{len(top_10_list)}** top articles.",
                "details": {
                    "batch_id": batch_id,
                    "target_site": target_site,
                    "total_articles": total,
                    "top_10_count": len(top_10_list),
                    "inventory_count": len(next_50)
                },
                "artifacts": [{
                    "filename": Path(top_10_path).name,
                    "type": "json",
                    "description": f"Curated Top 10 for {target_site}."
                }],
                "status_overrides": {
                    "PARTIAL": {
                        "description": "Some URLs failed validation, extraction, or embedding.",
                        "next_steps": f"Call the `scraper` tool again using exactly: {{\"target_site\": \"{target_site}\"}}. The system will skip completed URLs and retry only the failed ones.",
                        "rerunnable": True
                    },
                    "COMPLETED": {
                        "description": "Scrape successful and batch generated.",
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
            return json.dumps(payload, ensure_ascii=False)

        except Exception as exc:
            if str(exc).startswith("PAUSED_FOR_HITL:"):
                raise
            await telemetry(self.status("Scraper failed.", "ERROR"))
            
            err_payload = {
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": "FAILED",
                "summary": f"Scraper execution crashed: {exc}",
                "status_overrides": {
                    "FAILED": {
                        "description": "Scraping crashed fatally.",
                        "next_steps": "Do NOT retry the exact same parameters. Use a different target_site or review system logs.",
                        "rerunnable": False
                    }
                }
            }
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
