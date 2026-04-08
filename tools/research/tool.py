# tools/research/tool.py
"""
Research Tool - 8-Step Chain-of-Thought Analysis with PDF Generation

This module implements an institutional research tool that:
1. Scrapes content from a URL using headful browser automation
2. Runs 8 sequential LLM-driven analysis steps (with Map-Reduce for large content)
3. Generates a PDF report with ReportLab
4. Implements database-backed job caching for resume capability
5. Reports progress via TelemetryManager
"""

import os
import json
import time
import asyncio
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from botasaurus.browser import browser, Driver
from clients.llm import get_llm_client, LLMRequest
from tools.base import BaseTool, TelemetryCallback
from utils.text_processing import clean_html_for_agent, map_reduce_summarize
from tools.research.research_prompts import get_cot_prompt, get_all_steps
from tools.research.pdf_engine import ReportEngine
from utils.logger import get_dual_logger
from database.connection import DatabaseManager

log = get_dual_logger(__name__)
from database.writer import enqueue_write
from tools.research.curator import curate_report_knowledge
from tools.research.scraper_agent import AgenticBrowserScraper
from utils.vector_search import retrieve_relevant_memories, get_memory_context_string
from utils.browser_lock import browser_lock
import config

# Constants from config
RESEARCH_REPORT_DIR = os.getenv("RESEARCH_REPORT_DIR", "reports")


async def _fetch_institutional_context(query: str) -> str:
    '''
    Retrieves relevant Knowledge and Values memories for a given query
    and formats them as an XML context block for prompt injection.
    '''
    agent_domain = "institutional_research"
    knowledge = await retrieve_relevant_memories(
        query, agent_domain=agent_domain, memory_type='Knowledge', limit=5, threshold=0.55)
    values = await retrieve_relevant_memories(
        query, agent_domain=agent_domain, memory_type='Values', limit=3, threshold=0.45)
    combined = knowledge + values
    return get_memory_context_string(combined) if combined else ''


def scrape_content_headful(data: dict) -> str:
    """
    Synchronous function to scrape content from a URL using headful browser
    with intelligent recovery features.

    Args:
        data: Dict with 'url' key and optional 'cancellation_flag' threading.Event.

    Returns:
        Cleaned text content from the page, or the sentinel string '__CANCELED__'
        if the operator typed 'Stop' at the HITL prompt.
    """
    url = data.get('url')
    if not url:
        return ""
    cancellation_flag = data.get('cancellation_flag')   # threading.Event or None
    scraper = AgenticBrowserScraper(max_retries=3)
    return scraper.scrape(url, topic_hint="", cancellation_flag=cancellation_flag)


class ResearchTool(BaseTool):
    """
    Research Tool that implements 8-step CoT analysis with PDF generation.
    
    Input arguments:
        url (str, required): target URL to research
        goal (str, optional): research objective (defaults to institutional analysis)
    """
    
    name = "research"
    
    def is_resumable(self, args: dict[str, Any]) -> bool:
        """Return True if the tool supports mid-run resume for the given args."""
        return True
    
    # ── Job Cache Database Methods ────────────────────────────────────────
    @staticmethod
    def _get_cache_key(url: str, step_name: str = "") -> str:
        """Generate deterministic cache key from URL."""
        url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
        if step_name:
            return f"{url_hash}:{step_name}"
        return url_hash
    
    @staticmethod
    def _load_job_cache(*args, **kwargs) -> None:
        """Deprecated placeholder. Job cache has been migrated to the relational job queue.

        This function remains for backward compatibility but always returns None.
        Use database/job_queue.py to resume or inspect interrupted jobs.
        """
        return None
    
    @staticmethod
    def _save_job_cache(*args, **kwargs):
        """Deprecated placeholder that no longer persists to job_cache.

        The research tool persists progress to the relational job queue via
        create_job/add_job_item/update_item_status/update_job_heartbeat.
        """
        return None
    
    @staticmethod
    def _cleanup_job_cache(*args, **kwargs):
        """Deprecated placeholder for legacy job_cache cleanup."""
        return None
    
    async def run(self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs) -> str:
        """Singleton browser gate. Rejects concurrent callers immediately."""
        # Respect a caller-provided cancellation_flag if present (cooperative)
        cancellation_flag = kwargs.get("cancellation_flag") or threading.Event()

        # Pre-lock cancellation check — if the job was cancelled while queued, abort immediately.
        if cancellation_flag.is_set():
            return "__CANCELED__"

        if browser_lock.locked():
            return "⚠️ System is currently busy running another browser task. Please try again later."
        await browser_lock.acquire()
        try:
            return await self._run_internal(args, telemetry, **kwargs)
        finally:
            browser_lock.release()

    async def _run_internal(self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs) -> str:
        """Execute the research pipeline."""
        # ── DRY_RUN guard ───────────────────────────────────────────
        dry_run = kwargs.get('dry_run', config.TELEGRAM_DRY_RUN)
        if dry_run:
            log.dual_log(
                tag="Research:Tool",
                message=f'[DRY RUN] Would execute research for URL: {args.get("url", "")}',
                level="INFO",
                payload={'event_type': 'research.dry_run'}
            )
            return "[DRY RUN] Research tool execution skipped."

        # ── Extract session_id and chat_id from kwargs ───────────────────
        session_id = kwargs.get('session_id')
        chat_id = kwargs.get('chat_id')
        
        if not session_id:
            await telemetry(self.status("Session ID is required for state management", "ERROR"))
            return "Error: Session ID is required for the research tool."

        url = args.get("url")
        goal = args.get("goal", "Comprehensive Institutional Analysis")
        
        if not url:
            await telemetry(self.status("URL is required", "ERROR"))
            return "Error: URL is required for the research tool."
        
        # Step 1: Scrape content
        await telemetry(self.status(f"Navigating headful to {url}...", "RUNNING"))
        cancellation_flag = threading.Event()
        try:
            raw_text = await asyncio.to_thread(
                scrape_content_headful,
                {'url': url, 'cancellation_flag': cancellation_flag},
            )

            # ── Cancellation gate — must fire before any further processing ──
            if cancellation_flag.is_set() or raw_text == "__CANCELED__":
                _audit_hash = self._get_cache_key(url)   # url_hash is defined later; compute inline
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                os.makedirs(RESEARCH_REPORT_DIR, exist_ok=True)   # may not exist yet at this point
                audit_path = os.path.join(
                    RESEARCH_REPORT_DIR, f"audit_canceled_{_audit_hash}_{ts}.json"
                )
                with open(audit_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {"timestamp": ts, "url": url, "status": "CANCELED", "partial_xml": ""},
                        f, indent=2, ensure_ascii=False,
                    )
                await telemetry(self.status("Research canceled by user.", "ERROR"))
                # Mark job abandoned if a job_id was created/linked
                job_id = kwargs.get("job_id")
                if job_id:
                    enqueue_write("UPDATE jobs SET status = 'ABANDONED' WHERE job_id = ?", (job_id,))
                return "Research was canceled by the user via Human Help mode."
                # Map-Reduce, CoT loop, ReportEngine.generate(), curate_report_knowledge(),
                # and job cache cleanup are all bypassed by this return.
            # ── End cancellation gate ─────────────────────────────────────────

            # ── ADD: Black Box boundary log for raw scraped content ──
            log.dual_log(
                tag="Research:Tool:Scrape",
                message=f"Raw scraped text captured from {url}",
                payload={"raw_text": raw_text},
            )
            # ── END ADD ──
            if not raw_text or len(raw_text) < 100:
                await telemetry(self.status("Failed to extract sufficient content from URL", "ERROR"))
                return "Error: Could not extract sufficient content from the URL."
        except Exception as exc:
            await telemetry(self.status(f"Scrape failed: {exc}", "ERROR"))
            return f"### ❌ Scrape Error\n{exc}"
        
        # Step 2: Map-Reduce Context Ingestion (instead of hard truncation)
        await telemetry(self.status("Processing massive content with Map-Reduce...", "RUNNING"))
        
        # Check that all research dependencies are available
        if not all([get_cot_prompt, get_all_steps, ReportEngine]):
            await telemetry(self.status("Research modules not found", "ERROR"))
            return "Error: Internal research components (prompts/PDF engine) are missing."
        
        # Initialize LLM and PDF engine
        llm = get_llm_client(provider_type="azure")
        engine = ReportEngine()
        
        # Setup output directory
        os.makedirs(RESEARCH_REPORT_DIR, exist_ok=True)
        
        # Generate deterministic filename based on URL
        url_hash = self._get_cache_key(url)
        filename = f"research_{url_hash}.pdf"
        output_path = os.path.join(RESEARCH_REPORT_DIR, filename)
        
        # Resume from database cache if exists - use async wrapper for thread safety
        cache_key = self._get_cache_key(url, "research_pipeline")
        start_step = 0
        accumulated_xml = ""

        # Initialize job queue (relational) resume support
        from database.job_queue import create_job, add_job_item, update_item_status, update_job_heartbeat
        import sqlite3 as _sqlite3

        job_id = kwargs.get("job_id")
        if not job_id:
            # If chat_id is not provided, fall back to 0 (legacy/session-backed callers may omit it)
            _chat_for_job = chat_id if chat_id is not None else 0
            job_id = create_job(_chat_for_job, self.name, json.dumps(args))
        else:
            enqueue_write("UPDATE jobs SET status = 'RUNNING' WHERE job_id = ?", (job_id,))

        # Reconstruct progress from existing completed job_items
        _rconn = DatabaseManager.get_read_connection()
        _rconn.row_factory = _sqlite3.Row
        _done = _rconn.execute(
            "SELECT step_identifier, output_data FROM job_items WHERE job_id = ? AND status = 'COMPLETED' ORDER BY item_id ASC",
            (job_id,)
        ).fetchall()

        start_step = len(_done)
        accumulated_xml = ""
        for _row in _done:
            try:
                accumulated_xml += json.loads(_row['output_data']).get('xml', '')
            except Exception:
                pass

        if start_step > 0:
            await telemetry(self.status(f"Resuming from step {start_step}...", "RUNNING"))
        
        # Get all steps
        steps = get_all_steps()
        
        # Use Map-Reduce to handle massive content instead of truncation
        if len(raw_text) > 30000:
            await telemetry(self.status("Content too large, initiating Map-Reduce summarization...", "RUNNING"))
            try:
                # Summarize the scraped content first to fit in context window
                raw_text = await map_reduce_summarize(raw_text, llm, chunk_size=25000, overlap=2500)
                await telemetry(self.status(f"Map-Reduce complete. Reduced to {len(raw_text)} chars.", "RUNNING"))
            except Exception as exc:
                await telemetry(self.status(f"Map-Reduce failed: {exc}", "ERROR"))
                return f"### ❌ Map-Reduce Error\n{exc}"
        
        try:
            # Execute 8-step CoT pipeline
            step = "initialization"  # Safe default before the loop
            for i, step in enumerate(steps):
                if i < start_step:
                    continue  # Skip already completed steps
                
                await telemetry(self.status(f"Step {i+1}/8: {step.replace('_', ' ').title()}...", "RUNNING"))
                
                # Retrieve relevant institutional memories before each step
                institutional_context = ""
                try:
                    relevant_memories = await retrieve_relevant_memories(
                        query=f"{step}: {goal}",
                        agent_domain="institutional_research",
                        memory_type="Knowledge",
                        limit=3,
                        threshold=0.52
                    )
                    if relevant_memories:
                        institutional_context = get_memory_context_string(relevant_memories)
                        if institutional_context:
                            await telemetry(self.status(f"Retrieved {len(relevant_memories)} relevant memories for context", "RUNNING"))
                except Exception as e:
                    log.dual_log(tag="Research:Tool", message=f"RAG retrieval failed for step {step}: {e}", level="WARNING", payload={"step": step})
                
                # Generate prompt for this step with retrieved context
                from utils.text_processing import escape_prompt_separators
                prompt = get_cot_prompt(
                    step,
                    escape_prompt_separators(raw_text),
                    escape_prompt_separators(accumulated_xml),
                    escape_prompt_separators(institutional_context)
                )
                
                # Call LLM
                resp = await llm.complete_chat(LLMRequest(
                    messages=[{"role": "user", "content": prompt}],
                    reasoning_effort="medium"
                ))
                
                # Accumulate XML
                accumulated_xml += f"\n{resp.content}"
                
                # Save progress as a job_item and update heartbeat (relational job queue)
                cache_data = {"step": i + 1, "xml": accumulated_xml}
                try:
                    # Use the async-safe enqueue-based API to avoid FK race and writer contention
                    add_job_item(job_id, step, json.dumps({}))
                    update_item_status(job_id, step, 'COMPLETED', json.dumps({"xml": f"\n{resp.content}"}))
                    update_job_heartbeat(job_id)
                except Exception as e:
                    log.dual_log(tag="Research:Tool", message=f"Failed to persist job item: {e}", level="WARNING", payload={"job_id": job_id})
            
            # Step 9: Generate PDF
            await telemetry(self.status("Rendering PDF report...", "RUNNING"))

            # ── ADD: Black Box boundary log for final accumulated XML ──
            log.dual_log(
                tag="Research:Tool:XML",
                message="Final accumulated XML ready for PDF generation.",
                payload={"accumulated_xml": accumulated_xml},
            )
            # ── END ADD ──

            engine.generate(accumulated_xml, output_path)
            
            # Step 10: Knowledge Curation (sub-agent)
            await telemetry(self.status("Curating knowledge into long-term memory...", "RUNNING"))
            try:
                curation_result = await curate_report_knowledge(chat_id, accumulated_xml)
                curation_data = json.loads(curation_result)
                if "error" not in curation_data:
                    curated_count = curation_data.get("curated_count", 0)
                    await telemetry(self.status(f"Knowledge curation complete: {curated_count} insights stored", "RUNNING"))
                else:
                    await telemetry(self.status("Knowledge curation skipped (LLM error)", "RUNNING"))
            except Exception as e:
                log.dual_log(tag="Research:Tool", message=f"Knowledge curation failed but research continues: {e}", level="WARNING", payload={"chat_id": chat_id})
                # Don't fail the research if curation fails - just notify
                await telemetry(self.status("Knowledge curation failed, but report is complete", "RUNNING"))
            
            # Cleanup job_items for this job on success
            try:
                enqueue_write("DELETE FROM job_items WHERE job_id = ?", (job_id,))
            except Exception:
                pass
            
            # Success
            await telemetry(self.status("Report complete.", "SUCCESS"))
            
            # Return result with preview
            preview = accumulated_xml[:500] + "..." if len(accumulated_xml) > 500 else accumulated_xml
            return f"### ✅ Research Complete\nReport generated: `{output_path}`\n\n{preview}"
            
        except Exception as exc:
            # Error handling - attempt partial PDF
            await telemetry(self.status(f"Pipeline error: {exc}", "ERROR"))
            
            if accumulated_xml.strip():
                try:
                    engine.generate(accumulated_xml, output_path)
                    # Save partial result to cache
                    cache_data = {
                        "step": len(steps),  # Mark as completed as much as possible
                        "xml": accumulated_xml
                    }
                    # Persist partial result as job_item before returning
                    try:
                        add_job_item(job_id, f"partial_{step}", json.dumps({}))
                        update_item_status(job_id, f"partial_{step}", 'COMPLETED', json.dumps({"xml": accumulated_xml}))
                        update_job_heartbeat(job_id)
                    except Exception:
                        pass
                    await telemetry(self.status(f"Partial failure at step '{step}'; partial PDF saved.", "ERROR"))
                    return f"### ⚠️ Partial Report\nSaved to `{output_path}` (failed at step {step}).\nError: {exc}"
                except Exception:
                    pass
            
            return f"### ❌ Research Failed\n{exc}"
