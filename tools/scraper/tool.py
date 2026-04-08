# tools/scraper/tool.py
"""
Scraper Tool — adapted for AnythingTools.
- Writes artifacts under AnythingTools/artifacts/scrapes/
- Exposes a Pydantic INPUT_MODEL for API validation
- Returns ToolResult.attachment_paths containing relative artifact paths (under `artifacts/`)
"""
import os
import json
import asyncio
import threading
from datetime import datetime, timezone
from typing import Any
from pathlib import Path
import tempfile

import config
from pydantic import BaseModel
from tools.base import BaseTool, TelemetryCallback, ToolResult
from clients.llm import get_llm_client, LLMRequest
from utils.logger import get_dual_logger
from utils.browser_lock import browser_lock
from tools.scraper.task import _run_botasaurus_scraper
from utils.id_generator import ULID
from database.writer import enqueue_write

log = get_dual_logger(__name__)

# INPUT_MODEL exported for registry / FastAPI validation
class ScraperInput(BaseModel):
    target_site: str

INPUT_MODEL = ScraperInput


class ScraperTool(BaseTool):
    name = "scraper"

    def is_resumable(self, args: dict[str, Any]) -> bool:
        return True

    async def run(self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs) -> str:
        # Respect a caller-provided cancellation_flag if present (cooperative)
        cancellation_flag = kwargs.get("cancellation_flag") or threading.Event()

        # Pre-lock cancellation check — if the job was cancelled while queued, abort immediately.
        if cancellation_flag.is_set():
            return "__CANCELED__"

        if browser_lock.locked():
            return "System busy: another browser task is running."
        await browser_lock.acquire()
        try:
            return await self._run_internal(args, telemetry, cancellation_flag=cancellation_flag, **kwargs)
        finally:
            browser_lock.release()

    async def _run_internal(self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs) -> str:
        dry_run = kwargs.get("dry_run", config.TELEMETRY_DRY_RUN)
        if dry_run:
            return "[DRY RUN] Scraper tool execution skipped."

        loop = asyncio.get_running_loop()

        # Sync bridges for the browser thread
        def sync_telemetry(msg: str, state: str = "RUNNING"):
            try:
                fut = asyncio.run_coroutine_threadsafe(telemetry(self.status(msg, state)), loop)
                fut.result(timeout=5)
            except Exception:
                pass

        def sync_llm_chat(messages, response_format=None):
            async def _call():
                llm = get_llm_client("azure")
                return await llm.complete_chat(LLMRequest(messages=messages, response_format=response_format))

            return asyncio.run_coroutine_threadsafe(_call(), loop).result(timeout=300)

        target_site = args.get("target_site")
        if not target_site:
            return "Error: target_site argument is required."

        await telemetry(self.status(f"Launching headful scraper for {target_site}...", "RUNNING"))

        # Use provided cancellation flag or a fresh one
        cancellation_flag = kwargs.get("cancellation_flag") or threading.Event()

        try:
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
                },
            )

            # Persist results into artifacts root
            artifacts_root: Path = Path(config.ARTIFACTS_ROOT)
            output_dir: Path = artifacts_root / "scrapes"
            output_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filepath = output_dir / f"scraper_output_{ts}.json"

            with open(filepath, "w", encoding="utf-8") as fh:
                json.dump(results, fh, indent=2, ensure_ascii=False)

            _stats = results.pop("_stats", {"new": 0, "resumed_retried": 0, "skipped_complete": 0, "skipped_abandoned": 0, "success": 0, "fail": 0, "fail_reasons": []})

            # Build slim list & attempt curation
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

            await telemetry(self.status("Curating Top 10 articles...", "RUNNING"))
            batch_id = ULID.generate()
            top_10_list = []
            if slim_list:
                curation_prompt = (
                    "You are a financial intelligence curator.\nReturn ONLY a JSON object with key 'top_10' which is an array of ulids.\n" + json.dumps(slim_list)
                )
                try:
                    llm = get_llm_client("azure")
                    curate_res = await llm.complete_chat(LLMRequest(messages=[{"role": "user", "content": curation_prompt}], response_format={"type": "json_object"}))
                    top_10_ulids = json.loads(curate_res.content).get("top_10", [])[:10]
                    slim_index = {item["ulid"]: item for item in slim_list}
                    top_10_list = [slim_index[u] for u in top_10_ulids if u in slim_index]
                except Exception as _ce:
                    log.dual_log(tag="Scraper:Curation", message=f"Curation failed; falling back: {_ce}", level="WARNING", exc_info=_ce)
                    top_10_list = slim_list[:10]

            top_10_path = output_dir / f"top_10_{batch_id}.json"
            with tempfile.NamedTemporaryFile("w", dir=output_dir, delete=False, suffix=".tmp", encoding="utf-8") as _tf:
                json.dump(top_10_list, _tf, indent=2, ensure_ascii=False)
                _tmp_name = _tf.name
            os.replace(_tmp_name, top_10_path)

            # Record artifact relative path under artifacts root for the worker
            rel_top_10 = str(Path("artifacts") / "scrapes" / top_10_path.name)
            # Store on instance for execute() wrapper to surface
            self._last_artifacts = [rel_top_10]

            # Enqueue a broadcast_batches record (legacy compatibility, non-fatal)
            try:
                enqueue_write(
                    "INSERT INTO broadcast_batches (batch_id, target_site, raw_json_path, curated_json_path, status) VALUES (?, ?, ?, ?, 'PENDING')",
                    (batch_id, target_site, str(filepath), str(top_10_path)),
                )
            except Exception:
                log.dual_log(tag="Scraper:DBInsert", message="Failed to enqueue broadcast_batches insert", level="WARNING")

            await telemetry(self.status("Scraper and curation finished.", "SUCCESS"))

            # Return a human-friendly execution report (the execute() wrapper will
            # also return structured artifact paths in ToolResult.attachment_paths)
            return f"Scrape complete. batch_id={batch_id}. Top {len(top_10_list)} curated."

        except Exception as exc:
            log.dual_log(tag="Scraper:Run", message=f"Scraper failed: {exc}", level="ERROR", exc_info=exc)
            await telemetry(self.status("Scraper failed.", "ERROR"))
            return f"Scraper tool failed: {exc}"

    async def execute(self, args, telemetry, **kwargs) -> "ToolResult":
        # Call BaseTool.execute which manages ContextVar log buffering and diagnosis.
        base_res = await super().execute(args, telemetry, **kwargs)

        # Harvest any artifacts recorded during run and return them as relative
        # artifact paths under the artifacts/ root per AnythingTools contract.
        artifacts = getattr(self, "_last_artifacts", None)

        return ToolResult(
            output=base_res.output,
            success=base_res.success,
            attachment_paths=artifacts,
            event_id=base_res.event_id,
            diagnosis=base_res.diagnosis,
        )
