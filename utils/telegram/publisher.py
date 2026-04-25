# utils/telegram/publisher.py
import json
import config
from typing import List, Dict
from utils.logger import get_dual_logger
from utils.text_processing import escape_markdown_v2, smart_split_message
from utils.metadata_helpers import make_metadata, STEP_PUBLISH_BRIEFING, STEP_PUBLISH_ARCHIVE
from database.job_queue import add_job_item
from utils.telegram.telegram_client import TelegramAPIClient
from utils.telegram.state_manager import PhaseStateManager
from telegram.constants import ParseMode

log = get_dual_logger(__name__)

class ChannelPublisher:
    def __init__(self, client: TelegramAPIClient, state_mgr: PhaseStateManager, job_id: str | None):
        self.client = client
        self.state_mgr = state_mgr
        self.job_id = job_id
        self.briefing_chat = getattr(config, 'TELEGRAM_BRIEFING_CHAT_ID', None)
        self.archive_chat = getattr(config, 'TELEGRAM_ARCHIVE_CHAT_ID', None)
        self.max_message_length = getattr(config, 'TELEGRAM_MAX_MESSAGE_LENGTH', 4000)

    async def publish_briefing(self, articles: List[Dict], translated_map: Dict[str, Dict]) -> None:
        if not self.briefing_chat: return
        top_10 = [a for a in articles if a.get("_is_top10") and a.get("ulid") in translated_map]
        
        for article in top_10:
            ulid = article.get("ulid")
            if self.state_mgr.state.is_completed("publish_briefing", ulid):
                continue
                
            trans_data = translated_map.get(ulid, {})
            title = trans_data.get("translated_title", article.get("title", ""))
            summary = trans_data.get("translated_summary", article.get("summary", ""))
            conclusion = trans_data.get("translated_conclusion", article.get("conclusion", ""))
            link = article.get("normalized_url", article.get("url", ""))
            if not link:
                link = "URL Unavailable"
            
            meta = make_metadata(STEP_PUBLISH_BRIEFING, ulid, is_top10=True)
            if self.job_id: add_job_item(self.job_id, meta, "")

            raw_text = f"*{title}*\n\n{summary}\n\n*Kesimpulan:* {conclusion}"
            body_text = escape_markdown_v2(raw_text)

            # Send link first (no parse mode)
            err1 = await self.client.send_message(self.briefing_chat, link, parse_mode=None, disable_link_preview=True)
            if not err1.success and err1.is_transient:
                raise Exception(f"Transient limit hit: {err1.description}")
            
            # Send body
            chunks = smart_split_message(body_text, self.max_message_length, ParseMode.MARKDOWN_V2)
            all_chunks_success = True
            for chunk in chunks:
                err2 = await self.client.send_message(self.briefing_chat, chunk, parse_mode=ParseMode.MARKDOWN_V2, disable_link_preview=True)
                if not err2.success:
                    all_chunks_success = False
                    if err2.is_transient:
                        raise Exception(f"Transient limit hit: {err2.description}")

            if err1.success and all_chunks_success:
                self.state_mgr.state.mark_completed("publish_briefing", ulid)
                self.state_mgr.persist_atomic(self.job_id or "0", meta, "COMPLETED")
            else:
                self.state_mgr.state.mark_failed("publish_briefing", ulid)
                self.state_mgr.persist_atomic(self.job_id or "0", meta, "FAILED")

    async def publish_archive(self, articles: List[Dict], translated_map: Dict[str, Dict]) -> None:
        if not self.archive_chat: return
        archive_items = [a for a in articles if a.get("ulid") in translated_map]
        
        for article in archive_items:
            ulid = article.get("ulid")
            if self.state_mgr.state.is_completed("publish_archive", ulid):
                continue
                
            trans_data = translated_map.get(ulid, {})
            title = trans_data.get("translated_title", article.get("title", ""))
            summary = trans_data.get("translated_summary", article.get("summary", ""))
            conclusion = trans_data.get("translated_conclusion", article.get("conclusion", ""))
            link = article.get("normalized_url", article.get("url", ""))
            if not link:
                link = "URL Unavailable"

            meta = make_metadata(STEP_PUBLISH_ARCHIVE, ulid, is_top10=article.get("_is_top10", False))
            if self.job_id: add_job_item(self.job_id, meta, "")

            raw_text = f"*{title}*\n\n*Kesimpulan:* {conclusion}\n\n*Ringkasan:*\n{summary}"
            body_text = escape_markdown_v2(raw_text)

            # Send link first (no parse mode)
            err1 = await self.client.send_message(self.archive_chat, link, parse_mode=None, disable_link_preview=True)
            if not err1.success and err1.is_transient:
                raise Exception(f"Transient limit hit: {err1.description}")
            
            # Send body
            chunks = smart_split_message(body_text, self.max_message_length, ParseMode.MARKDOWN_V2)
            all_chunks_success = True
            for chunk in chunks:
                err2 = await self.client.send_message(self.archive_chat, chunk, parse_mode=ParseMode.MARKDOWN_V2, disable_link_preview=True)
                if not err2.success:
                    all_chunks_success = False
                    if err2.is_transient:
                        raise Exception(f"Transient limit hit: {err2.description}")

            if err1.success and all_chunks_success:
                self.state_mgr.state.mark_completed("publish_archive", ulid)
                self.state_mgr.persist_atomic(self.job_id or "0", meta, "COMPLETED")
            else:
                self.state_mgr.state.mark_failed("publish_archive", ulid)
                self.state_mgr.persist_atomic(self.job_id or "0", meta, "FAILED")
