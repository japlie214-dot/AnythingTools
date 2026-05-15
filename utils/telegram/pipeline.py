# utils/telegram/pipeline.py
import json
import config
from typing import Dict, List, Any
from utils.telegram.validator import ArticleValidator
from utils.telegram.translator import BatchTranslator
from utils.telegram.publisher import ChannelPublisher
from utils.telegram.telegram_client import TelegramAPIClient
from database.broadcast.queries import get_details_for_publish, get_batch_articles, get_batch_publish_progress, get_batch_info
from database.broadcast.writer import update_batch_status_from_details, update_detail_publish_status
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class PublisherPipeline:
    def __init__(self, batch_id: str, top_10: List[Dict], inventory: List[Dict], job_id: str | None = None, resume: bool = False, reset: bool = False):
        self.batch_id = batch_id
        self.top_10 = top_10
        self.inventory = inventory
        self.job_id = job_id
        self.resume = resume
        self.reset = reset

        bot_token = getattr(config, 'TELEGRAM_BOT_TOKEN', None)
        self.client = TelegramAPIClient(bot_token=bot_token) if bot_token else None
        self.all_articles = get_batch_articles(batch_id)

    async def run_pipeline(self) -> Dict[str, Any]:
        pending_articles = get_details_for_publish(self.batch_id) if (self.resume and not self.reset) else self.all_articles

        try:
            validator = ArticleValidator(self.job_id)
            valid_articles, skipped = validator.validate_batch(pending_articles)

            for skip_art in skipped:
                ulid = skip_art.get("ulid")
                if ulid:
                    update_detail_publish_status(self.batch_id, ulid, "SKIPPED")
        except Exception as e:
            import traceback
            log.dual_log(tag="Publisher:Pipeline:Crash", message=f"Validation phase crashed: {e}\n{traceback.format_exc()}", level="ERROR", exc_info=e, payload={"batch_id": self.batch_id, "job_id": self.job_id, "error": str(e)})
            raise RuntimeError(f"Publisher validation crashed: {e}") from e

        translator = BatchTranslator(self.job_id, self.batch_id)
        translated_map = await translator.translate_all(valid_articles)

        if self.client:
            publisher = ChannelPublisher(self.client, self.batch_id, self.job_id)
            await publisher.publish_briefing(valid_articles, translated_map)
            await publisher.publish_archive(valid_articles, translated_map)
            await self.client.close()

        update_batch_status_from_details(self.batch_id)
        progress = get_batch_publish_progress(self.batch_id)

        published_archive = progress.get("PUBLISHED_ARCHIVE", 0)
        published_briefing = progress.get("PUBLISHED_BRIEFING", 0)
        trans_failed = progress.get("FAILED", 0)
        total = len(self.all_articles)

        batch_info = get_batch_info(self.batch_id)
        batch_status = batch_info["status"] if batch_info else "FAILED"

        return {
            "batch_status": batch_status,
            "total_items": total,
            "skipped_items": progress.get("SKIPPED", 0),
            "translated": len(translated_map),
            "translation_failed": trans_failed,
            "briefing_posted": published_briefing,
            "archive_posted": published_archive,
            "failed_ulids": list(translator.failed_ulids),
        }
