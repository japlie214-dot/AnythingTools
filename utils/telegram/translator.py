# utils/telegram/translator.py
import json
import sqlite3
from collections import deque
from typing import Dict, List, Set, Any
import config
from clients.llm import get_llm_client, LLMRequest
from database.connection import DatabaseManager
from database.job_queue import add_job_item, update_item_status
from utils.logger import get_dual_logger
from utils.metadata_helpers import make_metadata, STEP_TRANSLATE
from tools.publisher.prompt import TRANSLATION_PROMPT
from utils.text_processing import parse_llm_json

log = get_dual_logger(__name__)
MAX_TRANSLATION_RETRIES = 3
BATCH_SIZE = 10

class BatchTranslator:
    def __init__(self, job_id: str | None):
        self.job_id = job_id
        self.translated_map: Dict[str, Dict] = {}
        self.failed_ulids: Set[str] = set()

    def load_cached(self, articles: List[Dict]) -> None:
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        for article in articles:
            ulid = article.get("ulid")
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

    async def translate_all(self, articles: List[Dict]) -> Dict[str, Dict]:
        self.load_cached(articles)
        queue: deque[Dict] = deque([a for a in articles if a.get("ulid") not in self.translated_map])
        if not queue:
            return self.translated_map

        retry_count: Dict[str, int] = {}
        # Load retry counts from job_items
        if self.job_id:
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

        iteration = 0
        safety_limit = len(queue) * MAX_TRANSLATION_RETRIES + 1

        while queue and iteration < safety_limit:
            iteration += 1
            current_batch = []
            while queue and len(current_batch) < BATCH_SIZE:
                article = queue.popleft()
                ulid = article.get("ulid")
                if retry_count.get(ulid, 0) >= MAX_TRANSLATION_RETRIES:
                    self.failed_ulids.add(ulid)
                    self._record_failure(article, retry_count.get(ulid, 0))
                else:
                    current_batch.append(article)

            if not current_batch:
                continue

            translations = await self._call_llm(current_batch)
            for article in current_batch:
                ulid = article.get("ulid")
                trans_data = translations.get(ulid)
                if trans_data and trans_data.get("translated_title"):
                    self.translated_map[ulid] = trans_data
                    if self.job_id:
                        meta = make_metadata(STEP_TRANSLATE, ulid, retry=retry_count.get(ulid, 0), model=getattr(config, 'AZURE_DEPLOYMENT', 'gpt-4o-mini'), is_top10=article.get("_is_top10", False))
                        add_job_item(self.job_id, meta, json.dumps(article, ensure_ascii=False))
                        update_item_status(self.job_id, meta, "COMPLETED", json.dumps(trans_data, ensure_ascii=False))
                else:
                    retry_count[ulid] = retry_count.get(ulid, 0) + 1
                    if retry_count[ulid] >= MAX_TRANSLATION_RETRIES:
                        self.failed_ulids.add(ulid)
                        self._record_failure(article, retry_count[ulid])
                    else:
                        queue.append(article)

        return self.translated_map

    async def _call_llm(self, batch: List[Dict]) -> Dict[str, Dict]:
        input_batch = [{"ulid": a.get("ulid", ""), "title": a.get("title", ""), "summary": a.get("summary", ""), "conclusion": a.get("conclusion", "")} for a in batch]
        prompt = TRANSLATION_PROMPT.format(input_json=json.dumps(input_batch, ensure_ascii=False))
        translations = {}
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
            log.dual_log(tag="Publisher:Translate:Error", message=f"LLM Error: {e}", level="WARNING")
        return translations

    def _record_failure(self, article: Dict, retry: int) -> None:
        if not self.job_id: return
        ulid = article.get("ulid", "UNKNOWN")
        meta = make_metadata(STEP_TRANSLATE, ulid, retry=retry, model=getattr(config, 'AZURE_DEPLOYMENT', 'gpt-4o-mini'), error="Max retries reached", is_top10=article.get("_is_top10", False))
        add_job_item(self.job_id, meta, json.dumps(article, ensure_ascii=False))
        update_item_status(self.job_id, meta, "FAILED", "{}")
