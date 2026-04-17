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
from utils.id_generator import ULID
from database.writer import append_to_ledger, enqueue_write

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
        cancellation_flag = kwargs.get("cancellation_flag") or threading.Event()
        if cancellation_flag.is_set():
            return "__CANCELED__"

        if browser_lock.locked():
            return "System busy: another browser task is running."
        
        await browser_lock.acquire()
        try:
            return await self._run_internal(args, telemetry, cancellation_flag=cancellation_flag, **kwargs)
        finally:
            browser_lock.safe_release()

    async def _run_internal(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        """Internal implementation with full pipeline."""
        dry_run = kwargs.get("dry_run", config.TELEMETRY_DRY_RUN)
        if dry_run:
            return "[DRY RUN] Scraper tool execution skipped."

        job_id = kwargs.get("job_id")
        session_id = str(kwargs.get("session_id") or kwargs.get("chat_id", "0"))
        target_site = args.get("target_site")
        
        if not target_site:
            return "Error: target_site argument is required."

        # 📝 LOG: Scout initialization to execution_ledger
        if job_id and session_id != "0":
            msg = f"The Scout: Starting extraction for {target_site}."
            append_to_ledger(job_id, session_id, "system", msg)

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
        cancellation_flag = kwargs.get("cancellation_flag") or threading.Event()

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

            # Persist raw results
            artifacts_root: Path = Path(config.ARTIFACTS_ROOT)
            output_dir: Path = artifacts_root / "scrapes"
            output_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filepath = output_dir / f"scraper_output_{ts}.json"
            with open(filepath, "w", encoding="utf-8") as fh:
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

            # Atomic save of Top 10
            top_10_path = output_dir / f"top_10_{batch_id}.json"
            with tempfile.NamedTemporaryFile("w", dir=output_dir, delete=False, suffix=".tmp", encoding="utf-8") as _tf:
                json.dump(top_10_list, _tf, indent=2, ensure_ascii=False)
                _tmp_name = _tf.name
            os.replace(_tmp_name, top_10_path)
            
            # Store artifact path for ToolResult
            self._last_artifacts = [str(Path("artifacts") / "scrapes" / top_10_path.name)]

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

            # 📝 LOG: Intelligent Manifest to execution_ledger
            if job_id and session_id != "0":
                manifest_content = "\n".join(manifest_lines)
                append_to_ledger(job_id, session_id, "system", manifest_content)

            # Save to broadcast_batches
            try:
                enqueue_write(
                    "INSERT INTO broadcast_batches (batch_id, target_site, raw_json_path, curated_json_path, status) VALUES (?, ?, ?, ?, 'PENDING')",
                    (batch_id, target_site, str(filepath), str(top_10_path))
                )
            except Exception:
                pass

            await telemetry(self.status("Scraper and curation finished.", "SUCCESS"))
            
            payload = {
                "message": f"### Scout Extraction Complete\n**Batch ID:** `{batch_id}`\nUse `batch_reader` to query inventory.",
                "batch_id": batch_id,
                "top_10": [{"title": item.get("title", ""), "summary": item.get("conclusion", ""), "ulid": item.get("ulid", "")} for item in top_10_list],
                "inventory": [{"title": item.get("title", ""), "ulid": item.get("ulid", "")} for item in next_50],
                "total_count": total
            }
            return json.dumps(payload, ensure_ascii=False)

        except Exception as exc:
            if str(exc).startswith("PAUSED_FOR_HITL:"):
                raise
            await telemetry(self.status("Scraper failed.", "ERROR"))
            return f"Scout Mode failed: {exc}"

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
