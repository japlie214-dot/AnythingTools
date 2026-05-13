# tools/scraper/tool.py
import json
import threading
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, List, Dict
import httpx
import config

from utils.logger import get_dual_logger
from utils.id_generator import ULID
from utils.browser_lock import browser_lock
from utils.metadata_helpers import make_metadata
from database.writer import enqueue_write
from database.job_queue import add_job_item, update_item_status

from tools.base import BaseTool
from tools.scraper.prompts import SCRAPER_SYS_PROMPT, CURATION_SYS_PROMPT
from tools.scraper.targets import VALID_TARGET_NAMES, TARGET_SITE_MAP

log = get_dual_logger(__name__)

# Import LLM client factory
from clients.llm.factory import get_llm_client, LLMRequest


# Checkpoint helpers: no-op shims to preserve legacy call-sites and avoid NameError.
# These intentionally force full linear execution when invoked.
def _check_step(step_name: str) -> bool:
    """Legacy checkpoint shim — always return False to force fresh execution of steps."""
    return False


def _get_step_output(step_name: str) -> dict:
    """Legacy checkpoint shim — return empty output (no cached step outputs)."""
    return {}

class ScraperTool(BaseTool):
    name = "scraper"
    description = "Scrape and curate top articles from a target site. Returns a curated top 10 list enriched with insights."
    input_model = None  # Dynamic validation in execute()

    async def run(self, args: dict[str, Any], telemetry: Any, job_id: str | None = None, session_id: str | None = None, cancellation_flag: threading.Event | None = None, dry_run: bool | None = None, **kwargs) -> str:
        """Execute the full scraper pipeline including extraction, curation, artifacts, and backup."""
        import threading
        cancellation_flag = cancellation_flag or threading.Event()
        
        if browser_lock.locked():
            payload = {
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": "FAILED",
                "summary": "System busy: another browser task is running.",
                "details": {},
            }
            return json.dumps(payload, ensure_ascii=False)
            
        browser_lock.acquire()
        try:
            return await self._run_internal(args, telemetry, job_id, session_id, cancellation_flag, dry_run, **kwargs)
        finally:
            browser_lock.release()

    async def _run_internal(self, args: dict[str, Any], telemetry: Any, job_id: str | None, session_id: str | None, cancellation_flag: threading.Event, dry_run: bool | None, **kwargs) -> str:
        """Internal implementation with full pipeline."""
        from utils.logger.structured import granular_log
        
        session_id = str(session_id or kwargs.get("chat_id", "0"))
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
                log.dual_log(
                    tag="Scraper:Validation:Failed",
                    message=summary,
                    level="WARNING",
                    status_state="FAILED",
                    payload={"job_id": job_id, "batch_id": batch_id, "target_site": target_site, "failure_summary": summary, "next_steps": next_steps}
                )
            except Exception:
                pass
            return json.dumps(payload, ensure_ascii=False)

        if dry_run is None:
            dry_run = config.TELEMETRY_DRY_RUN
        if dry_run:
            return _fail_internal("[DRY RUN] Scraper tool execution skipped.", "Disable dry run to execute.")

        if not target_site:
            return _fail_internal("Error: target_site argument is required.", "Provide a valid 'target_site' argument.")

        if target_site not in VALID_TARGET_NAMES:
            valid_list = ", ".join(sorted(VALID_TARGET_NAMES))
            log.dual_log(
                tag="Scraper:Validation:Rejected",
                message=f"Invalid target_site rejected: {target_site}",
                level="ERROR",
                payload={"received": target_site, "valid_options": list(VALID_TARGET_NAMES)},
            )
            return _fail_internal(f"Error: '{target_site}' is not a valid target site. Valid options: {valid_list}", f"Use one of the valid options: {valid_list}")

        # Scout initialization (legacy ledger removed) — log for auditing
        if job_id and session_id != "0":
            msg = f"The Scout: Starting extraction for {target_site}."
            log.dual_log(tag="Scraper:Lifecycle:Init", level="INFO", message=msg, payload={"job_id": job_id, "session_id": session_id, "batch_id": batch_id})

        loop = asyncio.get_running_loop()
        
        def sync_telemetry(msg: str, state: str = "RUNNING"):
            """Synchronous telemetry wrapper."""
            try:
                fut = asyncio.run_coroutine_threadsafe(telemetry(self.status(msg, state)), loop)
                fut.result(timeout=5)
            except Exception:
                pass

        def sync_llm_chat(messages, response_format=None, call_context=None):
            """Synchronous LLM wrapper for curation."""
            async def _call():
                llm = get_llm_client("azure")
                return await llm.complete_chat(LLMRequest(
                    messages=messages,
                    response_format=response_format,
                    call_context=call_context
                ))
            return asyncio.run_coroutine_threadsafe(_call(), loop).result(timeout=300)

        is_resume = kwargs.get("is_resume", False)
        if is_resume:
            await telemetry(self.status(f"Resuming headful scraper for {target_site}..."))
        else:
            await telemetry(self.status(f"Launching headful scraper for {target_site}..."))

        try:
            # Run Botasaurus pipeline
            from utils.browser_daemon import get_or_create_driver
            _scrape_driver = get_or_create_driver()
            
            with granular_log("Scraper:Botasaurus:Run", target_site=target_site, job_id=job_id):
                from tools.scraper.task import _run_botasaurus_scraper as _run_scraper
                results = await asyncio.to_thread(
                _run_scraper,
                _scrape_driver,
                {
                    "sync_telemetry": sync_telemetry,
                    "sync_llm_chat": sync_llm_chat,
                    "cancellation_flag": cancellation_flag,
                    "target_site": target_site,
                    "job_id": job_id,
                },
            )
            
            # Browser lock must cover the entire job lifecycle per GOLDEN RULE 2.
            # Do not release the browser lock early; it will be released in the outer run() finally block.
            pass

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
                _record_artifact(raw_filepath, "json", f"Raw scraper output for {target_site}")
                if job_id:
                    log.dual_log(
                        tag="Scraper:Extraction:Complete",
                        message=f"Extracted {len(results)} items",
                        level="INFO",
                        status_state="COMPLETED",
                        payload={"count": len(results), "batch_id": batch_id, "target_site": target_site, "job_id": job_id}
                    )
            except Exception as e:
                log.dual_log(
                    tag="Scraper:Artifact:Error",
                    message=f"Failed writing raw artifact: {e}",
                    level="WARNING",
                    payload={"error": str(e), "artifact_type": "raw_json", "target_site": target_site}
                )



            # Extraction Step
            extraction_meta = make_metadata("extract", batch_id)
            await telemetry(self.status("Validating extraction...", "RUNNING"))
            if job_id: add_job_item(job_id, extraction_meta, json.dumps({"target_site": target_site}))
            
            # results is a dict containing URLs mapped to article data, plus internal keys like _stats
            _stats = results.get("_stats", {})
            _job_final_status = results.get("_job_final_status", "COMPLETED")
            
            valid_results = {k: v for k, v in results.items() if not k.startswith("_")}
            if not valid_results:
                if job_id: update_item_status(job_id, extraction_meta, "FAILED", json.dumps({"error": "Empty results"}))
                return _fail_internal("No results extracted.", "Check target site validity and network connectivity.")
            if job_id: update_item_status(job_id, extraction_meta, "COMPLETED", json.dumps({"count": len(valid_results), "batch_id": batch_id}))

            # Extraction Step
            if cancellation_flag.is_set(): return _fail_internal("Scraper canceled before extraction.", "Job canceled.")

            slim_meta = make_metadata("slim", batch_id)
            await telemetry(self.status("Extracting article links...", "RUNNING"))
            if job_id: add_job_item(job_id, slim_meta, json.dumps({"target_site": target_site}))
            
            # Inline slim list construction
            slim_list = []
            for _url, _res in valid_results.items():
                if _res.get("status") == "SUCCESS" and _res.get("ulid"):
                    slim_list.append({
                        "ulid": _res["ulid"],
                        "normalized_url": _res.get("normalized_url", ""),
                        "title": _res.get("title", ""),
                        "conclusion": _res.get("conclusion", "")
                    })
            
            if job_id: update_item_status(job_id, slim_meta, "COMPLETED", json.dumps({"slim_count": len(slim_list), "batch_id": batch_id}))

            # Curation Step
            # Curation Step
            if cancellation_flag.is_set(): return _fail_internal("Scraper canceled before curation.", "Job canceled.")
            
            curate_meta = make_metadata("curate", batch_id)
            await telemetry(self.status("Curating top articles...", "RUNNING"))
            if job_id: add_job_item(job_id, curate_meta, "{}")
            
            top_10_list = []
            target_curated_count = 10
            if slim_list:
                try:
                    from tools.scraper.curation import Top10Curator
                    curator = Top10Curator()
                    top_10_list, target_curated_count = curator.curate(slim_list, sync_llm_chat, batch_id=batch_id)
                except Exception as _ce:
                    log.dual_log(
                        tag="Scraper:Curation:Execute",
                        message=f"Curation sub-agent failed; falling back to first 10: {_ce}",
                        level="WARNING",
                        exc_info=_ce
                    )
                    top_10_list = slim_list[:10]
                    target_curated_count = min(10, len(slim_list))
            
            if job_id: update_item_status(job_id, curate_meta, "COMPLETED", json.dumps({"top_10": top_10_list, "target_count": target_curated_count}))

            # Save Top 10 Artifact
            if cancellation_flag.is_set(): return _fail_internal("Scraper canceled before artifact generation.", "Job canceled.")
            
            art_meta = make_metadata("artifacts", batch_id)
            if job_id: add_job_item(job_id, art_meta, "{}")
            try:
                top_10_path = write_artifact(
                    tool_name="scraper", job_id=job_id or batch_id, artifact_type="top10", ext="json",
                    content=json.dumps(top_10_list, indent=2, ensure_ascii=False)
                )
                self._last_artifacts = [str(top_10_path)]
                _record_artifact(top_10_path, "json", f"Curated Top {target_curated_count} articles")
                if job_id: update_item_status(job_id, art_meta, "COMPLETED", json.dumps({"path": str(top_10_path)}))
            except Exception as e:
                log.dual_log(
                    tag="Scraper:Artifact:Error",
                    message=f"Failed: {e}",
                    level="WARNING",
                    payload={"error": str(e), "artifact_type": "top10_json", "target_site": target_site}
                )
                top_10_path = raw_filepath if raw_filepath else ""
                if job_id: update_item_status(job_id, art_meta, "FAILED", "{}")

            # Backup Sync Step - Acknowledgment
            if cancellation_flag.is_set(): return _fail_internal("Scraper canceled before backup acknowledgment.", "Job canceled.")
            
            log.dual_log(
                tag="Scraper:Backup:Inline",
                level="INFO",
                message="Article backup is handled inline by database/articles writer pipeline",
                payload={"batch_id": batch_id, "article_count": len(top_10_list)}
            )
            bak_res = None

            # Finalization
            if cancellation_flag.is_set(): return _fail_internal("Scraper canceled before finalization.", "Job canceled.")

            final_meta = make_metadata("finalize", batch_id)
            if job_id: add_job_item(job_id, final_meta, "{}")
            result_payload = {
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": "COMPLETED",
                "summary": f"Scraped and curated top {len(top_10_list)} articles from {target_site}",
                "details": {
                    "target_site": target_site,
                    "batch_id": batch_id,
                    "extracted_count": len(results),
                    "slim_count": len(slim_list),
                    "curated_count": len(top_10_list),
                    "target_curated_count": target_curated_count,
                    "artifacts_written": artifacts_written,
                },
                "artifacts": [str(a["filename"]) for a in artifacts_written],
                "backup_status": bak_res.dict() if bak_res else {"success": True, "message": "Disabled or skipped"}
            }
            if job_id: update_item_status(job_id, final_meta, "COMPLETED", json.dumps(result_payload))
            await telemetry(self.status("Completed", "COMPLETED", payload=result_payload))
            return json.dumps(result_payload, ensure_ascii=False)

        except Exception as e:
            log.dual_log(
                tag="Scraper:Unexpected:Error",
                message=f"Critical failure: {e}",
                level="ERROR",
                exc_info=e,
                payload={"error": str(e), "error_type": type(e).__name__, "target_site": target_site, "job_id": job_id}
            )
            return _fail_internal(f"Unexpected error: {str(e)}", "Contact administrator for investigation.")

    @property
    def last_artifacts(self) -> list[str]:
        return getattr(self, "_last_artifacts", [])


# Checkpoint helpers relocated to module top; no-op placeholder retained for compatibility.

def write_artifact(tool_name: str, job_id: str, artifact_type: str, ext: str, content: str) -> Path:
    """Write artifact to disk and return Path using atomic replacement.

    Raises an exception on failure so caller can surface fatal errors.
    """
    from pathlib import Path
    import os
    import tempfile

    # Create artifacts directory
    artifacts_dir = Path("artifacts") / tool_name
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename
    filename = f"{job_id}_{artifact_type}.{ext}"
    filepath = artifacts_dir / filename

    # Atomic write
    with tempfile.NamedTemporaryFile("w", dir=artifacts_dir, delete=False, suffix=".tmp", encoding="utf-8") as tf:
        tf.write(content)
        temp_name = tf.name
    os.replace(temp_name, filepath)

    return filepath
