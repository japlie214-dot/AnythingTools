"""utils/telegram_publisher.py

Producer-Consumer pipeline for parallel translation and Telegram delivery.
"""

import asyncio
import json
import httpx
from collections import deque
from dataclasses import dataclass
from typing import List, Dict, Any, Set, Tuple
from clients.llm import get_llm_client, LLMRequest
from database.job_queue import add_job_item, update_item_status
from database.connection import DatabaseManager
from database.writer import enqueue_write
import sqlite3
import config
from utils.logger import get_dual_logger
from utils.metadata_helpers import (
    make_metadata,
    parse_metadata,
    STEP_TRANSLATE,
    STEP_PUBLISH_BRIEFING,
    STEP_PUBLISH_ARCHIVE,
)

log = get_dual_logger(__name__)

MAX_TRANSLATION_RETRIES = 3
BATCH_SIZE = 10


@dataclass
class TelegramErrorInfo:
    success: bool
    is_permanent: bool = False
    is_transient: bool = False
    status_code: int = 0
    description: str = ""
    retry_after: int = 0

def _is_valid_article(article: Dict) -> Tuple[bool, str]:
    ulid = article.get("ulid")
    if ulid is None or ulid == "" or ulid == "None":
        return False, f"Invalid ULID: {ulid!r}"
    title = article.get("title")
    if not title or not isinstance(title, str) or not title.strip():
        return False, f"Missing/empty title for ULID {ulid}"
    return True, ""

class PublisherPipeline:
    def __init__(self, batch_id: str, top_10: List[Dict], inventory: List[Dict], job_id: str | None = None, resume: bool = False, reset: bool = False):
        self.batch_id = batch_id
        self.top_10 = top_10
        self.inventory = inventory
        self.job_id = job_id
        self.resume = resume
        self.reset = reset
        self.translated_map: Dict[str, Dict] = {}
        
        self.bot_token = getattr(config, 'TELEGRAM_BOT_TOKEN', None)
        self.briefing_chat = getattr(config, 'TELEGRAM_BRIEFING_CHAT_ID', None)
        self.archive_chat = getattr(config, 'TELEGRAM_ARCHIVE_CHAT_ID', None)
        self.message_delay = getattr(config, 'TELEGRAM_MESSAGE_DELAY', 3.1)
        self.max_message_length = getattr(config, 'TELEGRAM_MAX_MESSAGE_LENGTH', 4000)
        self.max_retry_after = getattr(config, 'TELEGRAM_MAX_RETRY_AFTER', 120)
        
        self.phase_state = {
            "validate": {}, "translate": {}, "publish_briefing": {}, "publish_archive": {}
        }
        
        self.translation_failed_ulids: Set[str] = set()
        self.valid_articles: List[Dict] = []
        self.skipped_articles: List[Dict] = []
        
        self._build_article_list()
        self._load_phase_state()

    def _load_phase_state(self):
        if self.reset:
            return
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT phase_state FROM broadcast_batches WHERE batch_id = ?", (self.batch_id,)).fetchone()
        if row and row["phase_state"]:
            try:
                loaded = json.loads(row["phase_state"])
                for phase in self.phase_state:
                    if phase in loaded:
                        self.phase_state[phase].update(loaded[phase])
            except Exception:
                pass

    def _build_article_list(self):
        self.all_articles = []
        top_10_ulids = {a.get("ulid") for a in self.top_10}
        for article in self.top_10:
            a_copy = article.copy()
            a_copy["_is_top10"] = True
            self.all_articles.append(a_copy)
        for article in self.inventory:
            if article.get("ulid") not in top_10_ulids:
                a_copy = article.copy()
                a_copy["_is_top10"] = False
                self.all_articles.append(a_copy)

    async def run_pipeline(self) -> Dict[str, Any]:
        log.dual_log(tag="Publisher:Pipeline:Start", message=f"Starting pipeline for batch {self.batch_id} ({len(self.all_articles)} items)")
        await self._phase0_validate()
        await self._phase1_translate_all()
        await self._phase2_upload_briefing()
        await self._phase3_upload_archive()
        return self._finalize()

    async def _phase0_validate(self) -> None:
        self.valid_articles = []
        self.skipped_articles = []
        for article in self.all_articles:
            is_valid, reason = _is_valid_article(article)
            if is_valid:
                self.valid_articles.append(article)
            else:
                self.skipped_articles.append(article)
                log.dual_log(tag="Publisher:Validate:Skip", message=f"Skipping invalid item in batch {self.batch_id}: {reason}", level="WARNING")
                if self.job_id:
                    ulid = article.get("ulid", "UNKNOWN")
                    meta = make_metadata("validate", ulid, error=f"Skipped: {reason}", is_top10=article.get("_is_top10", False))
                    add_job_item(self.job_id, meta, json.dumps(article, ensure_ascii=False))
                    update_item_status(self.job_id, meta, "FAILED", "{}")
        log.dual_log(tag="Publisher:Validate:Complete", message=f"Validation: {len(self.valid_articles)} valid, {len(self.skipped_articles)} skipped")

    @staticmethod
    def _validate_markdown_v2(text: str) -> bool:
        if not text: return True
        for marker in ['*', '_', '__', '~', '||']:
            if text.count(marker) % 2 != 0: return False
        if text.count('[') != text.count(']'): return False
        if text.count('(') != text.count(')'): return False
        return True

    async def _send_msg(self, client, chat_id, text, parse_mode="MarkdownV2") -> TelegramErrorInfo:
        from utils.text_processing import smart_split_message
        import re
        if not chat_id: return TelegramErrorInfo(success=False, is_permanent=True, description="No chat_id")
        
        if parse_mode == "MarkdownV2" and not self._validate_markdown_v2(text):
            log.dual_log(tag="Publisher:Send:Fallback", level="WARNING", message="MarkdownV2 validation failed; falling back to plain text")
            parse_mode = None
            text = re.sub(r'[*_~|]', '', text)

        chunks = smart_split_message(text, self.max_message_length, parse_mode)
        for chunk in chunks:
            retries = 0
            chunk_success = False
            while retries < 3:
                try:
                    resp = await client.post(
                        f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                        json={"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode, "disable_web_page_preview": False}
                    )
                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get("Retry-After", 5))
                        if retry_after > self.max_retry_after:
                            return TelegramErrorInfo(success=False, is_transient=True, retry_after=retry_after, description="Extreme Rate Limit")
                        await asyncio.sleep(retry_after)
                        retries += 1
                        continue
                    if resp.status_code in (400, 403):
                        log.dual_log(tag="Publisher:Send:Error", message=f"Permanent error: {resp.text}", level="ERROR")
                        return TelegramErrorInfo(success=False, is_permanent=True, status_code=resp.status_code, description=resp.text)
                    
                    resp.raise_for_status()
                    chunk_success = True
                    break
                except httpx.HTTPStatusError as e:
                    return TelegramErrorInfo(success=False, is_transient=True, status_code=e.response.status_code, description=str(e))
                except Exception as e:
                    return TelegramErrorInfo(success=False, is_transient=True, description=str(e))
            
            if not chunk_success:
                return TelegramErrorInfo(success=False, is_transient=True, description="Max retries exhausted")
                
            await asyncio.sleep(self.message_delay)
        return TelegramErrorInfo(success=True)

    async def _phase1_translate_all(self) -> None:
        self._load_cached_translations()
        
        queue: deque[Dict] = deque()
        for article in self.valid_articles:
            ulid = article.get("ulid")
            if ulid not in self.translated_map:
                queue.append(article)
                
        if not queue:
            log.dual_log(tag="Publisher:Translate:Complete", message="All items already cached.")
            return

        retry_count: Dict[str, int] = {}
        self._load_retry_counts(retry_count, queue)
        
        log.dual_log(tag="Publisher:Translate:Start", message=f"Translation queue: {len(queue)} items ({len(self.translated_map)} cached)")
        
        safety_limit = len(queue) * MAX_TRANSLATION_RETRIES + 1
        iteration = 0
        
        while queue and iteration < safety_limit:
            iteration += 1
            current_batch: List[Dict] = []
            skipped_ulids: List[str] = []
            
            while queue and len(current_batch) < BATCH_SIZE:
                article = queue.popleft()
                ulid = article.get("ulid")
                retries = retry_count.get(ulid, 0)
                
                if retries >= MAX_TRANSLATION_RETRIES:
                    skipped_ulids.append(ulid)
                    self.translation_failed_ulids.add(ulid)
                    self._record_translation_failure(article, retries)
                else:
                    current_batch.append(article)
                    
            for ulid in skipped_ulids:
                log.dual_log(tag="Publisher:Translate:Failed", message=f"Item {ulid} exhausted {MAX_TRANSLATION_RETRIES} retries, dropping.", level="ERROR")
                
            if not current_batch:
                continue
                
            log.dual_log(tag="Publisher:Translate:Batch", message=f"Sending batch of {len(current_batch)} items (iteration {iteration})")
            translations = await self._call_llm_translate_batch(current_batch)
            
            for article in current_batch:
                ulid = article.get("ulid")
                is_top10 = article.get("_is_top10", False)
                trans_data = translations.get(ulid)
                
                if trans_data and trans_data.get("translated_title"):
                    self.translated_map[ulid] = trans_data
                    if self.job_id:
                        meta = make_metadata(STEP_TRANSLATE, ulid, retry=retry_count.get(ulid, 0), model=getattr(config, 'AZURE_DEPLOYMENT', 'gpt-4o-mini'), is_top10=is_top10)
                        add_job_item(self.job_id, meta, json.dumps(article, ensure_ascii=False))
                        update_item_status(self.job_id, meta, "COMPLETED", json.dumps(trans_data, ensure_ascii=False))
                else:
                    retry_count[ulid] = retry_count.get(ulid, 0) + 1
                    if retry_count[ulid] >= MAX_TRANSLATION_RETRIES:
                        self.translation_failed_ulids.add(ulid)
                        self._record_translation_failure(article, retry_count[ulid])
                        log.dual_log(tag="Publisher:Translate:Failed", message=f"Item {ulid} exhausted {MAX_TRANSLATION_RETRIES} retries.", level="ERROR")
                    else:
                        queue.append(article)
                        log.dual_log(tag="Publisher:Translate:Requeue", message=f"Requeuing {ulid} (attempt {retry_count[ulid]}/{MAX_TRANSLATION_RETRIES})", level="WARNING")
                        
        log.dual_log(tag="Publisher:Translate:Complete", message=f"Translation phase done: {len(self.translated_map)} succeeded, {len(self.translation_failed_ulids)} failed")

    def _load_cached_translations(self) -> None:
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        for article in self.valid_articles:
            ulid = article.get("ulid")
            # Query independently of job_id to allow cross-job resumes
            row = conn.execute(
                "SELECT output_data FROM job_items WHERE json_extract(item_metadata, '$.step') = 'translate' AND json_extract(item_metadata, '$.ulid') = ? AND status = 'COMPLETED' ORDER BY updated_at DESC LIMIT 1",
                (ulid,)
            ).fetchone()
            if row and row["output_data"]:
                try:
                    data = json.loads(row["output_data"])
                    if data.get("translated_title"):
                        self.translated_map[ulid] = data
                except Exception:
                    pass

    def _load_retry_counts(self, retry_count: Dict[str, int], queue: deque) -> None:
        if not self.job_id: return
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        for article in queue:
            ulid = article.get("ulid")
            row = conn.execute(
                "SELECT item_metadata FROM job_items WHERE job_id = ? AND json_extract(item_metadata, '$.step') = 'translate' AND json_extract(item_metadata, '$.ulid') = ? AND status = 'FAILED'",
                (self.job_id, ulid)
            ).fetchone()
            if row and row["item_metadata"]:
                try:
                    meta = json.loads(row["item_metadata"])
                    if meta.get("retry", 0) > 0:
                        retry_count[ulid] = meta.get("retry")
                except Exception:
                    pass

    def _record_translation_failure(self, article: Dict, retry: int) -> None:
        if not self.job_id: return
        ulid = article.get("ulid", "UNKNOWN")
        is_top10 = article.get("_is_top10", False)
        meta = make_metadata(STEP_TRANSLATE, ulid, retry=retry, model=getattr(config, 'AZURE_DEPLOYMENT', 'gpt-4o-mini'), error="Max retries reached", is_top10=is_top10)
        add_job_item(self.job_id, meta, json.dumps(article, ensure_ascii=False))
        update_item_status(self.job_id, meta, "FAILED", "{}")

    async def _call_llm_translate_batch(self, batch: List[Dict]) -> Dict[str, Dict[str, str]]:
        from tools.publisher.prompt import TRANSLATION_PROMPT
        from utils.text_processing import parse_llm_json
        
        input_batch = [{"ulid": a.get("ulid", ""), "title": a.get("title", ""), "summary": a.get("summary", ""), "conclusion": a.get("conclusion", "")} for a in batch]
        prompt = TRANSLATION_PROMPT.format(input_json=json.dumps(input_batch, ensure_ascii=False))
        
        translations: Dict[str, Dict[str, str]] = {}
        try:
            llm = get_llm_client("azure")
            resp = await llm.complete_chat(LLMRequest(messages=[{"role": "user", "content": prompt}], response_format={"type": "json_object"}))
            
            parsed = parse_llm_json(resp.content)
            t_list = parsed.get("translations", parsed.get("articles", []))
            if not t_list and isinstance(parsed, dict) and len(parsed) > 0:
                t_list = list(parsed.values())[0]
            if isinstance(t_list, list):
                for t in t_list:
                    if isinstance(t, dict) and "ulid" in t:
                        translations[t["ulid"]] = {
                            "translated_title": t.get("translated_title", ""),
                            "translated_summary": t.get("translated_summary", ""),
                            "translated_conclusion": t.get("translated_conclusion", "")
                        }
        except Exception as e:
            log.dual_log(tag="Publisher:Translate:Error", message=f"LLM API/Parse Error: {e}", level="WARNING")
        return translations

    async def _phase2_upload_briefing(self) -> None:
        from utils.text_processing import escape_markdown_v2
        if not self.briefing_chat: return
        top_10_translated = [a for a in self.valid_articles if a.get("_is_top10") and a.get("ulid") in self.translated_map]
        
        if not top_10_translated:
            log.dual_log(tag="Publisher:Briefing:Skip", message="No translated top-10 items to upload.")
            return
            
        log.dual_log(tag="Publisher:Briefing:Start", message=f"Uploading {len(top_10_translated)} items to briefing")
        async with httpx.AsyncClient() as client:
            for article in top_10_translated:
                ulid = article.get("ulid")
                
                if self.phase_state["publish_briefing"].get(ulid, {}).get("status") == "COMPLETED":
                    continue

                trans_data = self.translated_map.get(ulid, {})
                title = trans_data.get("translated_title", article.get("title", ""))
                summary = trans_data.get("translated_summary", article.get("summary", ""))
                conclusion = trans_data.get("translated_conclusion", article.get("conclusion", ""))
                link = article.get("normalized_url", article.get("url", ""))
                
                if not title and not conclusion:
                    continue

                if self.job_id:
                    meta = make_metadata(STEP_PUBLISH_BRIEFING, ulid, is_top10=True)
                    add_job_item(self.job_id, meta, "")
                
                # Assemble text with intended structural formatting (MarkdownV2) and localized "Kesimpulan:"
                raw_text = f"*{title}*\n\n{summary}\n\n*Kesimpulan:* {conclusion}"
                body_text = escape_markdown_v2(raw_text)
                
                err1 = await self._send_msg(client, self.briefing_chat, link, parse_mode=None)
                if not err1.success and err1.retry_after > self.max_retry_after:
                    raise Exception(f"Aborting batch due to extreme rate limit ({err1.retry_after}s)")
                
                err2 = await self._send_msg(client, self.briefing_chat, body_text)
                if not err2.success and err2.retry_after > self.max_retry_after:
                    raise Exception(f"Aborting batch due to extreme rate limit ({err2.retry_after}s)")
                
                if err1.success and err2.success:
                    self.phase_state["publish_briefing"][ulid] = {"status": "COMPLETED"}
                    enqueue_write(
                        "UPDATE broadcast_batches SET phase_state = ?, updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
                        (json.dumps(self.phase_state, ensure_ascii=False), self.batch_id)
                    )
                    if self.job_id:
                        update_item_status(self.job_id, meta, "COMPLETED", "{}")
                else:
                    self.phase_state["publish_briefing"][ulid] = {"status": "FAILED"}
                    enqueue_write(
                        "UPDATE broadcast_batches SET phase_state = ?, updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
                        (json.dumps(self.phase_state, ensure_ascii=False), self.batch_id)
                    )
                    err_desc = err1.description if not err1.success else err2.description
                    log.dual_log(tag="Publisher:Briefing:Failed", message=f"Briefing upload failed for {ulid}: {err_desc}", level="WARNING")
                    if self.job_id:
                        update_item_status(self.job_id, meta, "FAILED", "{}")

        posted_count = sum(1 for v in self.phase_state["publish_briefing"].values() if v.get("status") == "COMPLETED")
        log.dual_log(tag="Publisher:Briefing:Complete", message=f"Briefing: {posted_count} posted")

    async def _phase3_upload_archive(self) -> None:
        from utils.text_processing import escape_markdown_v2
        if not self.archive_chat: return
        archive_items = [a for a in self.valid_articles if a.get("ulid") in self.translated_map]
        
        if not archive_items:
            log.dual_log(tag="Publisher:Archive:Skip", message="No translated items to upload to archive.")
            return
            
        log.dual_log(tag="Publisher:Archive:Start", message=f"Uploading {len(archive_items)} items to archive")
        async with httpx.AsyncClient() as client:
            for article in archive_items:
                ulid = article.get("ulid")
                
                if self.phase_state["publish_archive"].get(ulid, {}).get("status") == "COMPLETED":
                    continue

                trans_data = self.translated_map.get(ulid, {})
                title = trans_data.get("translated_title", article.get("title", ""))
                summary = trans_data.get("translated_summary", article.get("summary", ""))
                conclusion = trans_data.get("translated_conclusion", article.get("conclusion", ""))
                link = article.get("normalized_url", article.get("url", ""))
                
                if not title and not conclusion and not summary:
                    continue

                if self.job_id:
                    meta = make_metadata(STEP_PUBLISH_ARCHIVE, ulid, is_top10=article.get("_is_top10", False))
                    add_job_item(self.job_id, meta, "")
                
                # Assemble text with intended structural formatting (MarkdownV2) and localized Indonesian labels
                raw_text = f"*{title}*\n\n*Kesimpulan:* {conclusion}\n\n*Ringkasan:*\n{summary}"
                body_text = escape_markdown_v2(raw_text)
                
                err1 = await self._send_msg(client, self.archive_chat, link, parse_mode=None)
                if not err1.success and err1.retry_after > self.max_retry_after:
                    raise Exception(f"Aborting batch due to extreme rate limit ({err1.retry_after}s)")
                
                err2 = await self._send_msg(client, self.archive_chat, body_text)
                if not err2.success and err2.retry_after > self.max_retry_after:
                    raise Exception(f"Aborting batch due to extreme rate limit ({err2.retry_after}s)")
                
                if err1.success and err2.success:
                    self.phase_state["publish_archive"][ulid] = {"status": "COMPLETED"}
                    enqueue_write(
                        "UPDATE broadcast_batches SET phase_state = ?, updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
                        (json.dumps(self.phase_state, ensure_ascii=False), self.batch_id)
                    )
                    if self.job_id:
                        update_item_status(self.job_id, meta, "COMPLETED", "{}")
                else:
                    self.phase_state["publish_archive"][ulid] = {"status": "FAILED"}
                    enqueue_write(
                        "UPDATE broadcast_batches SET phase_state = ?, updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
                        (json.dumps(self.phase_state, ensure_ascii=False), self.batch_id)
                    )
                    err_desc = err1.description if not err1.success else err2.description
                    log.dual_log(tag="Publisher:Archive:Failed", message=f"Archive upload failed for {ulid}: {err_desc}", level="WARNING")
                    if self.job_id:
                        update_item_status(self.job_id, meta, "FAILED", "{}")

        posted_count = sum(1 for v in self.phase_state["publish_archive"].values() if v.get("status") == "COMPLETED")
        log.dual_log(tag="Publisher:Archive:Complete", message=f"Archive: {posted_count} posted")

    def _finalize(self) -> Dict[str, Any]:
        total = len(self.all_articles)
        skipped = len(self.skipped_articles)
        translated = len(self.translated_map)
        trans_failed = len(self.translation_failed_ulids)
        
        briefing_posted = sum(1 for v in self.phase_state["publish_briefing"].values() if v.get("status") == "COMPLETED")
        archive_posted = sum(1 for v in self.phase_state["publish_archive"].values() if v.get("status") == "COMPLETED")
        
        all_valid_translated = translated == len(self.valid_articles)
        all_briefing_posted = all(self.phase_state["publish_briefing"].get(a.get("ulid"), {}).get("status") == "COMPLETED" for a in self.valid_articles if a.get("_is_top10"))
        all_archive_posted = all(self.phase_state["publish_archive"].get(a.get("ulid"), {}).get("status") == "COMPLETED" for a in self.valid_articles)
        
        if len(self.valid_articles) == 0:
            batch_status = "FAILED"
        elif all_valid_translated and all_briefing_posted and all_archive_posted:
            batch_status = "COMPLETED"
        elif translated == 0 and trans_failed > 0:
            batch_status = "FAILED"
        else:
            batch_status = "PARTIAL"
            
        enqueue_write(
            "UPDATE broadcast_batches SET status = ?, phase_state = ?, updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
            (batch_status, json.dumps(self.phase_state, ensure_ascii=False), self.batch_id)
        )
        
        failed_items = sorted(list(self.translation_failed_ulids))
        log.dual_log(tag="Publisher:Finalize:Summary", message=f"Batch {self.batch_id} finished: {archive_posted}/{total} items published, {trans_failed} translation failures (items: {failed_items}), {skipped} skipped in validation")
        
        return {
            "batch_status": batch_status,
            "total_items": total,
            "skipped_items": skipped,
            "translated": translated,
            "translation_failed": trans_failed,
            "briefing_posted": briefing_posted,
            "archive_posted": archive_posted,
            "failed_ulids": failed_items,
        }
