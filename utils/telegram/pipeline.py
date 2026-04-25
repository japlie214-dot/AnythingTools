# utils/telegram/pipeline.py
import json
import sqlite3
import config
from typing import Dict, List, Any
from utils.telegram.validator import ArticleValidator
from utils.telegram.translator import BatchTranslator
from utils.telegram.publisher import ChannelPublisher
from utils.telegram.state_manager import PhaseStateManager
from utils.telegram.telegram_client import TelegramAPIClient
from database.writer import enqueue_write
from database.connection import DatabaseManager
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class PublisherPipeline:
    def __init__(self, batch_id: str, top_10: List[Dict], inventory: List[Dict], job_id: str | None = None, resume: bool = False, reset: bool = False):
        self.batch_id = batch_id
        self.top_10 = top_10
        self.inventory = inventory
        self.job_id = job_id
        self.state_mgr = PhaseStateManager(batch_id)
        
        bot_token = getattr(config, 'TELEGRAM_BOT_TOKEN', None)
        self.client = TelegramAPIClient(bot_token=bot_token) if bot_token else None
        
        self._build_article_list()
        if not reset:
            self._load_state()

    def _build_article_list(self):
        self.all_articles = []
        top_10_ulids = {a.get("ulid") for a in self.top_10}
        for a in self.top_10:
            a_copy = a.copy()
            a_copy["_is_top10"] = True
            self.all_articles.append(a_copy)
        for a in self.inventory:
            if a.get("ulid") not in top_10_ulids:
                a_copy = a.copy()
                a_copy["_is_top10"] = False
                self.all_articles.append(a_copy)

    def _load_state(self):
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT phase_state FROM broadcast_batches WHERE batch_id = ?", (self.batch_id,)).fetchone()
        if row and row["phase_state"]:
            try:
                loaded = json.loads(row["phase_state"])
                for phase in self.state_mgr.state.__dict__:
                    if phase in loaded:
                        getattr(self.state_mgr.state, phase).update(loaded[phase])
            except Exception:
                pass

    async def run_pipeline(self) -> Dict[str, Any]:
        try:
            validator = ArticleValidator(self.job_id)
            valid_articles, skipped = validator.validate_batch(self.all_articles)
        except Exception as e:
            import traceback
            log.dual_log(tag="Publisher:Pipeline:Crash", message=f"Validation phase crashed: {e}\n{traceback.format_exc()}", level="ERROR", exc_info=e)
            raise RuntimeError(f"Publisher validation crashed: {e}") from e
        
        translator = BatchTranslator(self.job_id)
        translated_map = await translator.translate_all(valid_articles)

        if self.client:
            publisher = ChannelPublisher(self.client, self.state_mgr, self.job_id)
            await publisher.publish_briefing(valid_articles, translated_map)
            await publisher.publish_archive(valid_articles, translated_map)
            await self.client.close()

        total = len(self.all_articles)
        trans_failed = len(translator.failed_ulids)
        
        briefing_posted = sum(1 for v in self.state_mgr.state.publish_briefing.values() if v.get("status") == "COMPLETED")
        archive_posted = sum(1 for v in self.state_mgr.state.publish_archive.values() if v.get("status") == "COMPLETED")
        
        all_valid_translated = len(translated_map) == len(valid_articles)
        all_briefing_posted = all(self.state_mgr.state.is_completed("publish_briefing", a.get("ulid")) for a in valid_articles if a.get("_is_top10"))
        all_archive_posted = all(self.state_mgr.state.is_completed("publish_archive", a.get("ulid")) for a in valid_articles)
        
        if len(valid_articles) == 0 or (len(translated_map) == 0 and trans_failed > 0):
            batch_status = "FAILED"
        elif all_valid_translated and all_briefing_posted and all_archive_posted:
            batch_status = "COMPLETED"
        else:
            batch_status = "PARTIAL"

        enqueue_write(
            "UPDATE broadcast_batches SET status = ?, phase_state = ?, updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
            (batch_status, json.dumps(self.state_mgr.state.to_dict(), ensure_ascii=False), self.batch_id)
        )

        return {
            "batch_status": batch_status,
            "total_items": total,
            "skipped_items": len(skipped),
            "translated": len(translated_map),
            "translation_failed": trans_failed,
            "briefing_posted": briefing_posted,
            "archive_posted": archive_posted,
            "failed_ulids": list(translator.failed_ulids),
        }
