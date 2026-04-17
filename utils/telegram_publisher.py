"""utils/telegram_publisher.py

Producer-Consumer pipeline for parallel translation and Telegram delivery.
"""

import asyncio
import json
import httpx
from typing import List, Dict, Any
from clients.llm import get_llm_client, LLMRequest
import config
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


class PublisherPipeline:
    def __init__(self, batch_id: str, top_10: List[Dict], inventory: List[Dict]):
        self.batch_id = batch_id
        self.top_10 = top_10
        self.inventory = inventory
        self.queue = asyncio.Queue()
        
    async def _translate_chunk(self, items: List[Dict]) -> List[Dict]:
        llm = get_llm_client("azure")
        
        prompt = "Translate the following JSON array of articles into Bahasa Indonesia. Return EXACTLY a JSON array of objects, preserving the 'ulid' key, and providing 'translated_title', 'translated_conclusion', and 'translated_summary'.\n\n"
        prompt += json.dumps(items, ensure_ascii=False)
        
        try:
            resp = await llm.complete_chat(LLMRequest(
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            ))
            
            # Basic JSON extraction
            content = resp.content
            start = content.find("[")
            end = content.rfind("]")
            if start != -1 and end != -1:
                parsed = json.loads(content[start:end+1])
                return parsed
            elif "{" in content:
                parsed = json.loads(content)
                # Return explicit keys if present, otherwise return the first value safely
                if isinstance(parsed, dict):
                    if parsed.get("articles"):
                        return parsed.get("articles")
                    if parsed.get("data"):
                        return parsed.get("data")
                    return list(parsed.values())[0] if len(parsed) > 0 else []
                # Fallback for unexpected types
                return []
        except Exception as e:
            log.dual_log(tag="Publisher:Translator", message=f"Translation failed: {e}", level="ERROR")
            
        return []

    async def producer(self):
        """Translates articles in chunks of 10 and pushes to queue."""
        # Combine Top 10 and Inventory to translate everything
        all_articles = self.top_10 + self.inventory
        
        chunk_size = 10
        for i in range(0, len(all_articles), chunk_size):
            chunk = all_articles[i:i+chunk_size]
            translated = await self._translate_chunk(chunk)
            
            # Map translations back to original URLs/Status
            trans_map = {item.get("ulid"): item for item in translated if isinstance(item, dict)}
            
            for article in chunk:
                ulid = article.get("ulid")
                is_top10 = any(t.get("ulid") == ulid for t in self.top_10)
                trans_data = trans_map.get(ulid, {})
                
                enriched = {
                    "ulid": ulid,
                    "url": article.get("normalized_url", article.get("url", "")),
                    "is_top10": is_top10,
                    "title": trans_data.get("translated_title", article.get("title", "")),
                    "conclusion": trans_data.get("translated_conclusion", article.get("conclusion", "")),
                    "summary": trans_data.get("translated_summary", article.get("summary", ""))
                }
                await self.queue.put(enriched)
                
        # Signal completion
        await self.queue.put(None)

    async def consumer(self):
        """Uploads articles to Telegram respecting rate limits and destinations."""
        token = getattr(config, "TELEGRAM_BOT_TOKEN", None)
        chat_a = getattr(config, "TELEGRAM_BRIEFING_CHAT_ID", None)
        chat_b = getattr(config, "TELEGRAM_ARCHIVE_CHAT_ID", None)
        delay = getattr(config, "TELEGRAM_MESSAGE_DELAY", 2.0)
        
        if not token:
            log.dual_log(tag="Publisher:Consumer", message="Missing Telegram token", level="ERROR")
            # Consume queue to prevent deadlock
            while await self.queue.get() is not None:
                self.queue.task_done()
            return
            
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        
        async with httpx.AsyncClient() as client:
            while True:
                article = await self.queue.get()
                if article is None:
                    self.queue.task_done()
                    break
                    
                target_url = article["url"]
                is_top10 = article["is_top10"]
                title = article["title"]
                conclusion = article["conclusion"]
                summary = article["summary"]
                
                # Helper to send and delay
                async def send_msg(chat_id, text):
                    if not chat_id: return
                    try:
                        await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False})
                    except Exception as e:
                        log.dual_log(tag="Publisher:Consumer", message=f"Telegram API error: {e}", level="WARNING")
                    await asyncio.sleep(delay)

                # Format messages
                msg1_url = target_url
                msg2_briefing = f"<b>{title}</b>\n\n{conclusion}"
                msg2_archive = f"<b>{title}</b>\n\n<b>Conclusion:</b> {conclusion}\n\n<b>Summary:</b>\n{summary}"

                # Destination A (Briefing Channel) -> Top 10 Only
                if is_top10 and chat_a:
                    await send_msg(chat_a, msg1_url)
                    await send_msg(chat_a, msg2_briefing)
                    
                # Destination B (Archive Channel) -> All Articles
                if chat_b:
                    await send_msg(chat_b, msg1_url)
                    await send_msg(chat_b, msg2_archive)
                
                self.queue.task_done()

    async def run_pipeline(self):
        prod_task = asyncio.create_task(self.producer())
        cons_task = asyncio.create_task(self.consumer())
        try:
            await prod_task
        except Exception:
            # Ensure consumer receives sentinel so it can terminate cleanly
            try:
                await self.queue.put(None)
            except Exception:
                pass
            raise
        await cons_task
        return "Publisher Pipeline Complete."
