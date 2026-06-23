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
from utils.artifact_manager import write_artifact

from tools.base import BaseTool, ToolExecutionError, ToolValidationError
from tools.scraper.prompts import SCRAPER_SYS_PROMPT, CURATION_SYS_PROMPT
from tools.scraper.targets import VALID_TARGET_NAMES, TARGET_SITE_MAP

log = get_dual_logger(__name__)

# Import LLM client factory
from clients.llm.factory import get_llm_client, LLMRequest



class ScraperTool(BaseTool):
    name = "scraper"
    description = "Scrape and curate top articles from a target site. Returns a curated top 10 list enriched with insights."
    input_model = None  # Dynamic validation in execute()


    async def run(self, args: dict[str, Any], telemetry: Any, job_id: str | None = None, session_id: str | None = None, cancellation_flag: threading.Event | None = None, dry_run: bool | None = None, **kwargs) -> str:
        """Execute the full scraper pipeline including extraction, curation, artifacts, and backup."""
        import threading
        cancellation_flag = cancellation_flag or threading.Event()
        
        if browser_lock.locked():
            raise ToolExecutionError(
                "System busy: another browser task is running.",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Try again later."
            )
            
        browser_lock.acquire()
        try:
            return await self._run_internal(args, telemetry, job_id, session_id, cancellation_flag, dry_run, **kwargs)
        finally:
            browser_lock.release()

    async def _run_internal(self, args: dict[str, Any], telemetry: Any, job_id: str | None, session_id: str | None, cancellation_flag: threading.Event, dry_run: bool | None, **kwargs) -> str:
        """Internal implementation with full pipeline."""
        from utils.logger.structured import granular_log
        
        # ── ARCHITECTURE RULE: ARTIFACT-AS-RECEIPT ──────────────────────────
        # Artifact files written below are RECEIPTS for audit/debug only.
        # Operational data lives in broadcast_batches + broadcast_details + scraped_articles.
        # ─────────────────────────────────────────────────────────────────────

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

        def _fail_internal(summary: str, next_steps: str) -> None:
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
            raise ToolExecutionError(
                summary,
                tool_name=self.name,
                job_id=job_id,
                next_steps=next_steps,
            )

        def _merge_completed_articles(results: dict, job_id: str | None) -> dict:
            if not job_id:
                return results
            try:
                from database.connection import DatabaseManager
                conn = DatabaseManager.get_read_connection()
                completed = conn.execute(
                    "SELECT id, url, title, conclusion, summary "
                    "FROM scraped_articles WHERE url IN ("
                    "  SELECT json_extract(item_metadata, '$.ulid') "
                    "  FROM job_items WHERE job_id = ? AND status = 'COMPLETED' "
                    "  AND json_extract(item_metadata, '$.step') = 'scrape'"
                    ")",
                    (job_id,)
                ).fetchall()
                stats = results.get("_stats", {})
                added_count = 0
                for row in completed:
                    url = row["url"]
                    if url not in results:
                        results[url] = {
                            "status": "SUCCESS",
                            "ulid": row["id"],
                            "url": url,
                            "title": row["title"],
                            "conclusion": row["conclusion"],
                            "summary": row["summary"],
                        }
                        added_count += 1
                if "success" in stats:
                    stats["success"] += added_count
                    stats["skipped_complete"] = stats.get("skipped_complete", 0) + added_count
            except Exception as e:
                log.dual_log(tag="Scraper:Merge:Error", message=f"Failed to merge completed articles: {e}", level="WARNING", payload={"error": str(e)})
            return results

        def _build_slim_list(valid_res: dict) -> list[dict]:
            s_list = []
            for _url, _res in valid_res.items():
                if _res.get("status") == "SUCCESS" and _res.get("ulid"):
                    s_list.append({
                        "ulid": _res["ulid"],
                        "url": _res.get("url", ""),
                        "title": _res.get("title", ""),
                        "conclusion": _res.get("conclusion", ""),
                        "summary": _res.get("summary", "")
                    })
            return s_list

        if dry_run is None:
            dry_run = config.TELEMETRY_DRY_RUN
        if dry_run:
            _fail_internal("[DRY RUN] Scraper tool execution skipped.", "Disable dry run to execute.")

        if not target_site:
            _fail_internal("Error: target_site argument is required.", "Provide a valid 'target_site' argument.")

        if target_site not in VALID_TARGET_NAMES:
            valid_list = ", ".join(sorted(VALID_TARGET_NAMES))
            log.dual_log(
                tag="Scraper:Validation:Rejected",
                message=f"Invalid target_site rejected: {target_site}",
                level="ERROR",
                payload={"received": target_site, "valid_options": list(VALID_TARGET_NAMES)},
            )
            _fail_internal(f"Error: '{target_site}' is not a valid target site. Valid options: {valid_list}", f"Use one of the valid options: {valid_list}")

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

        from database.connection import DatabaseManager
        conn = DatabaseManager.get_read_connection()
        items_exist = conn.execute("SELECT 1 FROM job_items WHERE job_id = ? LIMIT 1", (job_id,)).fetchone() if job_id else None
        is_resume = items_exist is not None

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
            results = _merge_completed_articles(results, job_id)

            # Persist raw results to artifacts directory
            raw_filepath = None
            try:
                raw_filepath = write_artifact(
                    tool_name="scraper",
                    job_id=job_id or batch_id,
                    artifact_type="scraper_output",
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

            # Slim List Step
            if cancellation_flag.is_set(): return _fail_internal("Scraper canceled before processing candidate list.", "Job canceled.")

            slim_meta = make_metadata("slim", batch_id)
            await telemetry(self.status("Extracting article links...", "RUNNING"))
            if job_id: add_job_item(job_id, slim_meta, json.dumps({"target_site": target_site}))
            
            slim_list = _build_slim_list(valid_results)
            
            try:
                slim_path = write_artifact(
                    tool_name="scraper", job_id=job_id or batch_id, artifact_type="slim_candidates", ext="json",
                    content=json.dumps(slim_list, indent=2, ensure_ascii=False)
                )
                _record_artifact(slim_path, "json", f"Pre-curation candidate pool for {target_site}")
            except Exception as e:
                log.dual_log(tag="Scraper:Artifact:Error", message=f"Failed writing slim_list artifact: {e}", level="WARNING", payload={"error": str(e)})

            if job_id: update_item_status(job_id, slim_meta, "COMPLETED", json.dumps({"slim_count": len(slim_list), "batch_id": batch_id}))

            # Curation Step
            if cancellation_flag.is_set(): return _fail_internal("Scraper canceled before curation.", "Job canceled.")
            
            curate_meta = make_metadata("curate", batch_id)
            if job_id: add_job_item(job_id, curate_meta, "{}")
            
            top_10_list = []
            target_curated_count = 10
            fallback_used = False
            
            if slim_list:
                try:
                    from tools.scraper.curation import Top10Curator
                    curator = Top10Curator()
                    curation_result = await curator.curate(slim_list, telemetry, batch_id=batch_id)
                    top_10_list = curation_result.curated_list
                    target_curated_count = curation_result.target_count
                    fallback_used = curation_result.fallback_used
                    
                    if job_id:
                        update_item_status(job_id, curate_meta, "COMPLETED", json.dumps({
                            "top_10": top_10_list,
                            "target_count": target_curated_count,
                            "fallback_used": fallback_used
                        }))
                except Exception as _ce:
                    log.dual_log(
                        tag="Scraper:Curation:Execute",
                        message=f"Curation sub-agent crashed; falling back to first 10: {_ce}",
                        level="ERROR",
                        exc_info=_ce
                    )
                    top_10_list = slim_list[:10]
                    target_curated_count = min(10, len(slim_list))
                    fallback_used = True
                    if job_id:
                        update_item_status(job_id, curate_meta, "COMPLETED", json.dumps({
                            "top_10": top_10_list,
                            "target_count": target_curated_count,
                            "fallback_used": True,
                            "error": str(_ce)
                        }))

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
                message="Article + embedding cloud backup triggered inline via ArticleStore.upsert_article()",
                payload={"batch_id": batch_id, "article_count": len(top_10_list), "cloud_sync": "enabled"}
            )
            bak_res = None

            # Finalization
            if cancellation_flag.is_set(): return _fail_internal("Scraper canceled before finalization.", "Job canceled.")

            # ── Persist to broadcast tables ─────────────────────────────────────
            try:
                from database.broadcast.writer import create_broadcast_batch, add_broadcast_details_bulk
                top10_ids = {item.get("ulid") for item in top_10_list if item.get("ulid")}
                all_success_articles = [_res for _url, _res in valid_results.items() if _res.get("status") == "SUCCESS" and _res.get("ulid")]

                create_broadcast_batch(
                    batch_id=batch_id,
                    target_site=target_site,
                    article_count=len(all_success_articles),
                    top10_count=len(top10_ids),
                    source_job_id=job_id,
                )
                add_broadcast_details_bulk(
                    batch_id=batch_id,
                    articles=all_success_articles,
                    top10_list=top_10_list,
                )
            except Exception as e:
                log.dual_log(tag="Scraper:Broadcast:WriteError", message=f"Failed to write broadcast tables: {e}", level="ERROR", exc_info=e, payload={"batch_id": batch_id, "error": str(e)})

            # ── Build enriched summary ──────────────────────────────────────────
            summary_parts = [f"Scraped and curated top {len(top_10_list)} articles from {target_site} (Batch ID: {batch_id}).\nSorted based on potential global impact."]
            
            if top_10_list:
                summary_parts.append("\n### Top 10 Curated Articles")
                for idx, article in enumerate(top_10_list, 1):
                    ulid = article.get("ulid", "unknown")
                    title = article.get("title", "Untitled")
                    conclusion = article.get("conclusion", "")
                    summary_text = article.get("summary", "")
                    summary_parts.append(f"\n**{idx}. [{ulid}] {title}**\nConclusion: {conclusion}\nSummary: {summary_text}")
                    
            top10_ulid_set = {item.get("ulid") for item in top_10_list}
            rest_articles = [res for res in valid_results.values() if res.get("status") == "SUCCESS" and res.get("ulid") and res.get("ulid") not in top10_ulid_set]
            if rest_articles:
                summary_parts.append("\n\n### Other Articles")
                for idx, article in enumerate(rest_articles, 1):
                    ulid = article.get("ulid", "unknown")
                    title = article.get("title", "Untitled")
                    if len(title) > 120: title = title[:117] + "..."
                    summary_parts.append(f"{idx}. [{ulid}] {title}")
                    
            enriched_summary = "\n".join(summary_parts)

            final_meta = make_metadata("finalize", batch_id)
            if job_id: add_job_item(job_id, final_meta, "{}")
            result_payload = {
                "_callback_format": "structured",
                "tool_name": self.name,
                "status": "COMPLETED",
                "summary": enriched_summary,
                "details": {
                    "target_site": target_site,
                    "batch_id": batch_id,
                    "extracted_count": len(results),
                    "slim_count": len(slim_list),
                    "curated_count": len(top_10_list),
                    "target_curated_count": target_curated_count,
                    "fallback_used": fallback_used if 'fallback_used' in locals() else False,
                    "artifacts_written": artifacts_written,
                    "artifacts_directory": "scraper",
                },
                "artifacts": artifacts_written,
                "backup_status": (bak_res.model_dump() if hasattr(bak_res, "model_dump") else bak_res.dict()) if bak_res else {"success": True, "message": "Disabled or skipped"}
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



