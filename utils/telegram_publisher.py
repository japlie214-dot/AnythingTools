"""utils/telegram_publisher.py

Producer-Consumer pipeline for parallel translation and Telegram delivery.
"""

import asyncio
import json
import httpx
from typing import List, Dict, Any
from clients.llm import get_llm_client, LLMRequest
from database.job_queue import add_job_item, update_item_status
from database.connection import DatabaseManager
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


class PublisherPipeline:
    def __init__(self, batch_id: str, top_10: List[Dict], inventory: List[Dict], job_id: str | None = None):
        self.batch_id = batch_id
        self.top_10 = top_10
        self.inventory = inventory
        self.job_id = job_id
        self.translated_map: Dict[str, Dict] = {}
        self.bot_token = getattr(config, 'TELEGRAM_BOT_TOKEN', None)
        self.briefing_chat = getattr(config, 'TELEGRAM_BRIEFING_CHAT_ID', None)
        self.archive_chat = getattr(config, 'TELEGRAM_ARCHIVE_CHAT_ID', None)
        self.message_delay = getattr(config, 'TELEGRAM_MESSAGE_DELAY', 3.1)
        self._build_article_list()

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

    async def run_pipeline(self) -> str:
        log.dual_log(tag="Publisher:Pipeline", message=f"Starting pipeline for batch {self.batch_id}")
        await self._phase1_translate_all()
        await self._phase2_upload_briefing()
        await self._phase3_upload_archive()
        return "Publisher Pipeline Complete."

    async def _get_uncached_articles(self) -> List[Dict]:
        if not self.job_id: return self.all_articles
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        uncached = []
        for article in self.all_articles:
            ulid = article.get("ulid")
            row = conn.execute(
                "SELECT output_data FROM job_items WHERE job_id = ? AND json_extract(item_metadata, '$.step') = 'translate' AND json_extract(item_metadata, '$.ulid') = ? AND status = 'COMPLETED'",
                (self.job_id, ulid)
            ).fetchone()
            if row and row["output_data"]:
                try:
                    self.translated_map[ulid] = json.loads(row["output_data"])
                except Exception:
                    uncached.append(article)
            else:
                uncached.append(article)
        return uncached

    async def _phase1_translate_all(self) -> None:
        MAX_RETRIES = 3
        for attempt in range(MAX_RETRIES):
            uncached = await self._get_uncached_articles()
            if not uncached:
                break
            log.dual_log(tag="Publisher:Phase1", message=f"Translation pass {attempt+1}/{MAX_RETRIES}. {len(uncached)} remaining.")
            for i in range(0, len(uncached), 10):
                batch = uncached[i:i+10]
                await self._translate_batch(batch, attempt, MAX_RETRIES)

    async def _translate_batch(self, batch: List[Dict], current_attempt: int, max_retries: int) -> None:
        if not batch: return
        model_name = getattr(config, 'AZURE_DEPLOYMENT', 'gpt-4o-mini')
        
        input_batch = [{"ulid": a.get("ulid", ""), "title": a.get("title", ""), "summary": a.get("summary", ""), "conclusion": a.get("conclusion", "")} for a in batch]
        prompt = "Translate the following JSON array of articles into Bahasa Indonesia. Return EXACTLY a JSON object with a 'translations' key containing an array of objects. Preserve 'ulid', and translate to 'translated_title', 'translated_summary', 'translated_conclusion'.\n" + json.dumps(input_batch, ensure_ascii=False)
        
        translations = {}
        try:
            llm = get_llm_client("azure")
            resp = await llm.complete_chat(LLMRequest(messages=[{"role": "user", "content": prompt}], response_format={"type": "json_object"}))
            content = resp.content
            start = content.find("{")
            end = content.rfind("}") + 1
            if start != -1 and end > start:
                parsed = json.loads(content[start:end])
                t_list = parsed.get("translations", parsed.get("articles", []))
                if not t_list and isinstance(parsed, dict) and len(parsed) > 0:
                    t_list = list(parsed.values())[0]
                if isinstance(t_list, list):
                    for t in t_list:
                        if isinstance(t, dict) and "ulid" in t:
                            translations[t["ulid"]] = {"translated_title": t.get("translated_title", ""), "translated_summary": t.get("translated_summary", ""), "translated_conclusion": t.get("translated_conclusion", "")}
        except Exception as e:
            log.dual_log(tag="Publisher:Translate", message=f"API/Parse Error: {e}", level="WARNING")

        for article in batch:
            ulid = article.get("ulid")
            is_top10 = article.get("_is_top10", False)
            if ulid in translations and translations[ulid].get("translated_title"):
                trans_data = translations[ulid]
                self.translated_map[ulid] = trans_data
                if self.job_id:
                    meta = make_metadata(STEP_TRANSLATE, ulid, retry=current_attempt, model=model_name, is_top10=is_top10)
                    add_job_item(self.job_id, meta, json.dumps(article))
                    update_item_status(self.job_id, meta, "COMPLETED", json.dumps(trans_data, ensure_ascii=False))
            else:
                if current_attempt >= max_retries - 1:
                    log.dual_log(tag="Publisher:Translate", message=f"Giving up on {ulid}", level="ERROR")
                    if self.job_id:
                        meta = make_metadata(STEP_TRANSLATE, ulid, retry=current_attempt+1, model=model_name, error="Max retries reached", is_top10=is_top10)
                        add_job_item(self.job_id, meta, json.dumps(article))
                        update_item_status(self.job_id, meta, "FAILED", "{}")
                else:
                    log.dual_log(tag="Publisher:Translate", message=f"Translation missing for {ulid}, will retry.", level="WARNING")

    async def _send_msg(self, client, chat_id, text) -> bool:
        if not chat_id: return False
        success = False
        try:
            resp = await client.post(f"https://api.telegram.org/bot{self.bot_token}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False})
            resp.raise_for_status()
            success = True
        except Exception as e:
            log.dual_log(tag="Publisher:Send", message=f"Telegram API error: {e}", level="WARNING")
        await asyncio.sleep(self.message_delay)
        return success

    async def _phase2_upload_briefing(self) -> None:
        if not self.top_10 or not self.briefing_chat: return
        async with httpx.AsyncClient() as client:
            for article in self.top_10:
                ulid = article.get("ulid")
                
                if self.job_id:
                    conn = DatabaseManager.get_read_connection()
                    conn.row_factory = sqlite3.Row
                    row = conn.execute("SELECT status FROM job_items WHERE job_id = ? AND json_extract(item_metadata, '$.step') = 'publish_briefing' AND json_extract(item_metadata, '$.ulid') = ?", (self.job_id, ulid)).fetchone()
                    if row and row["status"] == "COMPLETED":
                        continue

                trans_data = self.translated_map.get(ulid, {})
                title = trans_data.get("translated_title", article.get("title", ""))
                conclusion = trans_data.get("translated_conclusion", article.get("conclusion", ""))
                link = article.get("normalized_url", article.get("url", ""))
                if not title and not conclusion: continue

                if self.job_id:
                    meta = make_metadata(STEP_PUBLISH_BRIEFING, ulid, is_top10=True)
                    add_job_item(self.job_id, meta, "")
                
                s1 = await self._send_msg(client, self.briefing_chat, link)
                s2 = await self._send_msg(client, self.briefing_chat, f"<b>{title}</b>\n\n{conclusion}")
                
                if self.job_id:
                    if s1 and s2:
                        update_item_status(self.job_id, meta, "COMPLETED", "{}")
                    else:
                        update_item_status(self.job_id, meta, "FAILED", "{}")

    async def _phase3_upload_archive(self) -> None:
        if not self.all_articles or not self.archive_chat: return
        async with httpx.AsyncClient() as client:
            for article in self.all_articles:
                ulid = article.get("ulid")
                
                if self.job_id:
                    conn = DatabaseManager.get_read_connection()
                    conn.row_factory = sqlite3.Row
                    row = conn.execute("SELECT status FROM job_items WHERE job_id = ? AND json_extract(item_metadata, '$.step') = 'publish_archive' AND json_extract(item_metadata, '$.ulid') = ?", (self.job_id, ulid)).fetchone()
                    if row and row["status"] == "COMPLETED":
                        continue

                trans_data = self.translated_map.get(ulid, {})
                title = trans_data.get("translated_title", article.get("title", ""))
                summary = trans_data.get("translated_summary", article.get("summary", ""))
                conclusion = trans_data.get("translated_conclusion", article.get("conclusion", ""))
                link = article.get("normalized_url", article.get("url", ""))
                if not title and not conclusion and not summary: continue

                if self.job_id:
                    meta = make_metadata(STEP_PUBLISH_ARCHIVE, ulid)
                    add_job_item(self.job_id, meta, "")
                
                s1 = await self._send_msg(client, self.archive_chat, link)
                s2 = await self._send_msg(client, self.archive_chat, f"<b>{title}</b>\n\n<b>Conclusion:</b> {conclusion}\n\n<b>Summary:</b>\n{summary}")
                
                if self.job_id:
                    if s1 and s2:
                        update_item_status(self.job_id, meta, "COMPLETED", "{}")
                    else:
                        update_item_status(self.job_id, meta, "FAILED", "{}")
