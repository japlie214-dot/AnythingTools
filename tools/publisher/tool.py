# tools/publisher/tool.py
import asyncio
import json
import os
import sqlite3
from typing import Any

from telegram.error import RetryAfter, TelegramError

import config
from database.connection import DatabaseManager
from database.writer import enqueue_write
from database.job_queue import add_job_item, update_item_status
from clients.llm import get_llm_client, LLMRequest
from tools.base import BaseTool, TelemetryCallback
from utils.logger import get_dual_logger
from utils.text_processing import smart_split_telegram_message

log = get_dual_logger(__name__)


class PublisherTool(BaseTool):
    name = "publisher"

    async def run(
        self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs
    ) -> str:
        batch_id = args.get("batch_id")
        if not batch_id:
            return "Error: batch_id is required."

        # Dry-run guard — consistent with all other tools.
        if kwargs.get("dry_run", config.TELEGRAM_DRY_RUN):
            return "[DRY RUN] Publisher tool execution skipped."

        bot = kwargs.get("bot")
        if not bot:
            return "Error: Bot instance is not available in kwargs."

        # ── Load batch record ──────────────────────────────────────────────
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row

        batch_row = conn.execute(
            "SELECT raw_json_path, curated_json_path, status, "
            "       posted_research_ulids, posted_summary_ulids "
            "FROM broadcast_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()

        if not batch_row:
            return f"Error: No broadcast batch found for ID '{batch_id}'."

        if batch_row["status"] == "COMPLETED":
            return f"Notice: Batch '{batch_id}' has already been fully broadcast."

        raw_json_path     = batch_row["raw_json_path"]
        curated_json_path = batch_row["curated_json_path"]
        # Load exclusion sets once at entry; all appends occur only in the DB.
        posted_research: set[str] = set(
            json.loads(batch_row["posted_research_ulids"])
        )
        posted_summary: set[str] = set(
            json.loads(batch_row["posted_summary_ulids"])
        )

        # Get job_id for caching translations
        job_id = kwargs.get("job_id")
        llm = get_llm_client("azure")

        async def _translate_content(text: str, step_id: str) -> str:
            """Translate text to Bahasa Indonesia with job queue caching."""
            if not text.strip():
                return text
            
            # Check cache first
            if job_id:
                row = conn.execute(
                    "SELECT output_data FROM job_items WHERE job_id = ? AND step_identifier = ? AND status = 'COMPLETED'",
                    (job_id, step_id)
                ).fetchone()
                if row:
                    try:
                        return json.loads(row["output_data"]).get("translated_text", text)
                    except Exception:
                        pass
            
            # Build instruction-first translation prompt and sanitize input
            from utils.text_processing import escape_prompt_separators
            prompt = (
                "Translate the provided text into natural Bahasa Indonesia.\n\n"
                "INSTRUCTIONS:\n"
                "1. Retain all original HTML formatting and structure exactly as they appear.\n"
                "2. Output exclusively a valid JSON object with a single key 'translated_text'.\n\n"
                f"### TEXT TO TRANSLATE\n{escape_prompt_separators(text)}\n###"
            )

            try:
                resp = await llm.complete_chat(LLMRequest(
                    messages=[{"role": "user", "content": prompt}],
                    model=config.AZURE_TRANSLATOR_DEPLOYMENT,
                    response_format={"type": "json_object"}
                ))
                trans_text = json.loads(resp.content).get("translated_text", text)
            except Exception as e:
                log.dual_log(tag="Publisher:Translate", message=f"Translation failed: {e}", level="WARNING")
                trans_text = text
            
            # Cache the translation
            if job_id:
                try:
                    add_job_item(job_id, step_id, "")
                    update_item_status(job_id, step_id, "COMPLETED", json.dumps({"translated_text": trans_text}))
                except Exception as e:
                    log.dual_log(tag="Publisher:Cache", message=f"Failed to cache translation: {e}", level="WARNING")
            
            return trans_text

        enqueue_write(
            "UPDATE broadcast_batches SET status = 'PUBLISHING', "
            "updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
            (batch_id,),
        )

        try:
            # ── Phase 1: Research Channel ──────────────────────────────────
            await telemetry(
                self.status("Publishing to Research channel...", "RUNNING")
            )

            if not os.path.exists(raw_json_path):
                log.dual_log(
                    tag="Publisher",
                    message=f"raw_json_path not found: {raw_json_path}",
                    level="WARNING",
                )
            else:
                with open(raw_json_path, "r", encoding="utf-8") as fh:
                    raw_results: dict = json.load(fh)

                for _key, res in raw_results.items():
                    if _key.startswith("_"):
                        continue
                    if res.get("status") not in ("SUCCESS", "SUCCESS_NO_PARSE"):
                        continue

                    ulid = res.get("ulid")
                    if not ulid or ulid in posted_research:
                        continue

                    # Use the original article URL (preserves query parameters);
                    # fall back to normalized_url if url is absent.
                    link    = res.get("url") or res.get("normalized_url", "")
                    summary = res.get("summary", "")
                    
                    translated_summary = await _translate_content(summary, f"trans_p1_{ulid}")

                    # Phase 3: Initialise sub-article state from the authoritative DB row.
                    # Prefer output_data (latest update_item_status write) over input_data.
                    _p1_step = f"pub_p1_{ulid}"
                    _item_meta = {"link_sent": False, "summary_sent": False}
                    if job_id:
                        _p1_row = conn.execute(
                            "SELECT output_data, input_data FROM job_items "
                            "WHERE job_id = ? AND step_identifier = ?",
                            (job_id, _p1_step),
                        ).fetchone()
                        if _p1_row:
                            _stored = _p1_row["output_data"] or _p1_row["input_data"]
                            if _stored:
                                _item_meta.update(json.loads(_stored))
                        else:
                            add_job_item(job_id, _p1_step, json.dumps(_item_meta))

                    # 1. Dispatch Link (skip if already confirmed sent).
                    if not _item_meta["link_sent"]:
                        log.dual_log(
                            tag="Telegram:Send:Request",
                            message=f"P1 Link dispatch: {ulid}",
                            payload={"chat_id": config.PUBLISHER_RESEARCH_CHANNEL_ID, "url": link},
                        )
                        try:
                            _p1_sent = await bot.send_message(
                                chat_id=config.PUBLISHER_RESEARCH_CHANNEL_ID,
                                text=link,
                            )
                        except TelegramError as e:
                            raise  # Link is plain text; parse errors are impossible — propagate all.
                        log.dual_log(
                            tag="Telegram:Send:Response",
                            message=f"P1 Link sent: {ulid}",
                            payload={"message_id": _p1_sent.message_id, "date": str(_p1_sent.date)},
                        )
                        # Local state update first, then fire-and-forget DB persist.
                        _item_meta["link_sent"] = True
                        if job_id:
                            update_item_status(job_id, _p1_step, "RUNNING", json.dumps(_item_meta))
                        await asyncio.sleep(config.PUBLISHER_MESSAGE_DELAY_SECONDS)

                    # 2. Dispatch Translated Summary chunks (skip if already confirmed sent).
                    if not _item_meta["summary_sent"]:
                        _chunks = smart_split_telegram_message(
                            translated_summary, parse_mode=config.TELEGRAM_PARSE_MODE
                        )
                        for _chunk in _chunks:
                            log.dual_log(
                                tag="Telegram:Send:Request",
                                message=f"P1 Summary chunk dispatch: {ulid}",
                                payload={
                                    "chat_id": config.PUBLISHER_RESEARCH_CHANNEL_ID,
                                    "text": _chunk,
                                },
                            )
                            try:
                                _p1_sent = await bot.send_message(
                                    chat_id=config.PUBLISHER_RESEARCH_CHANNEL_ID,
                                    text=_chunk,
                                    parse_mode=config.TELEGRAM_PARSE_MODE,
                                )
                            except TelegramError as e:
                                if "parse" in str(e).lower() or "entities" in str(e).lower():
                                    _p1_sent = await bot.send_message(
                                        chat_id=config.PUBLISHER_RESEARCH_CHANNEL_ID,
                                        text=_chunk,
                                        parse_mode=None,
                                    )
                                else:
                                    raise
                            log.dual_log(
                                tag="Telegram:Send:Response",
                                message=f"P1 Summary chunk sent: {ulid}",
                                payload={
                                    "message_id": _p1_sent.message_id,
                                    "date": str(_p1_sent.date),
                                },
                            )
                            await asyncio.sleep(config.PUBLISHER_MESSAGE_DELAY_SECONDS)
                        _item_meta["summary_sent"] = True
                        if job_id:
                            update_item_status(job_id, _p1_step, "COMPLETED", json.dumps(_item_meta))

                    # Existing article-level idempotency guard — unchanged.
                    enqueue_write(
                        "UPDATE broadcast_batches "
                        "SET posted_research_ulids = "
                        "    json_insert(posted_research_ulids, '$[#]', ?), "
                        "    updated_at = CURRENT_TIMESTAMP "
                        "WHERE batch_id = ?",
                        (ulid, batch_id),
                    )

            # ── Phase 2: Summary Channel ───────────────────────────────────
            await telemetry(
                self.status("Publishing to Summary channel...", "RUNNING")
            )

            if not os.path.exists(curated_json_path):
                log.dual_log(
                    tag="Publisher",
                    message=f"curated_json_path not found: {curated_json_path}",
                    level="WARNING",
                )
            else:
                with open(curated_json_path, "r", encoding="utf-8") as fh:
                    top_10: list[dict] = json.load(fh)

                for item in top_10:
                    ulid = item.get("ulid")
                    if not ulid or ulid in posted_summary:
                        continue

                    link    = item.get("normalized_url", "")
                    title   = item.get("title", "")
                    concl   = item.get("conclusion", "")
                    
                    text_to_translate = f"<b>{title}</b>\n{concl}"
                    translated_text = await _translate_content(text_to_translate, f"trans_p2_{ulid}")

                    # Phase 3: Initialise sub-article state from the authoritative DB row.
                    # Summary channel dispatches one combined message; a single flag suffices.
                    _p2_step = f"pub_p2_{ulid}"
                    _s2_meta = {"message_sent": False}
                    if job_id:
                        _p2_row = conn.execute(
                            "SELECT output_data, input_data FROM job_items "
                            "WHERE job_id = ? AND step_identifier = ?",
                            (job_id, _p2_step),
                        ).fetchone()
                        if _p2_row:
                            _stored2 = _p2_row["output_data"] or _p2_row["input_data"]
                            if _stored2:
                                _s2_meta.update(json.loads(_stored2))
                        else:
                            add_job_item(job_id, _p2_step, json.dumps(_s2_meta))

                    if not _s2_meta["message_sent"]:
                        payload = (
                            f"{translated_text}\n"
                            f'<a href="{link}">Read Article</a>'
                        )
                        log.dual_log(
                            tag="Telegram:Send:Request",
                            message=f"P2 Summary dispatch: {ulid}",
                            payload={"chat_id": config.PUBLISHER_SUMMARY_CHANNEL, "text": payload},
                        )
                        try:
                            _p2_sent = await bot.send_message(
                                chat_id=config.PUBLISHER_SUMMARY_CHANNEL,
                                text=payload,
                                parse_mode=config.TELEGRAM_PARSE_MODE,
                            )
                        except TelegramError as e:
                            if "parse" in str(e).lower() or "entities" in str(e).lower():
                                plain_payload = f"{title}\n{concl}\nLink: {link}"
                                _p2_sent = await bot.send_message(
                                    chat_id=config.PUBLISHER_SUMMARY_CHANNEL,
                                    text=plain_payload,
                                    parse_mode=None,
                                )
                            else:
                                raise
                        log.dual_log(
                            tag="Telegram:Send:Response",
                            message=f"P2 Summary sent: {ulid}",
                            payload={
                                "message_id": _p2_sent.message_id,
                                "date": str(_p2_sent.date),
                            },
                        )
                        await asyncio.sleep(config.PUBLISHER_MESSAGE_DELAY_SECONDS)
                        _s2_meta["message_sent"] = True
                        if job_id:
                            update_item_status(job_id, _p2_step, "COMPLETED", json.dumps(_s2_meta))

                    enqueue_write(
                        "UPDATE broadcast_batches "
                        "SET posted_summary_ulids = "
                        "    json_insert(posted_summary_ulids, '$[#]', ?), "
                        "    updated_at = CURRENT_TIMESTAMP "
                        "WHERE batch_id = ?",
                        (ulid, batch_id),
                    )

            # ── Mark complete ──────────────────────────────────────────────
            enqueue_write(
                "UPDATE broadcast_batches "
                "SET status = 'COMPLETED', updated_at = CURRENT_TIMESTAMP "
                "WHERE batch_id = ?",
                (batch_id,),
            )
            await telemetry(self.status("Publishing completed.", "SUCCESS"))
            return f"Publishing completed successfully for batch_id={batch_id}."

        except RetryAfter as exc:
            enqueue_write(
                "UPDATE broadcast_batches "
                "SET status = 'PARTIAL', updated_at = CURRENT_TIMESTAMP "
                "WHERE batch_id = ?",
                (batch_id,),
            )
            await telemetry(self.status("FloodWait — publishing paused.", "ERROR"))
            return (
                f"Publishing paused: Telegram FloodWait. "
                f"Please wait {exc.retry_after} seconds, then ask me to resume "
                f"publisher with batch_id={batch_id}."
            )

        except Exception as exc:
            log.dual_log(
                tag="Publisher",
                message="Publishing failed with unexpected error.",
                level="ERROR",
                exc_info=exc,
            )
            enqueue_write(
                "UPDATE broadcast_batches "
                "SET status = 'PARTIAL', updated_at = CURRENT_TIMESTAMP "
                "WHERE batch_id = ?",
                (batch_id,),
            )
            await telemetry(self.status(f"Publishing error: {exc}", "ERROR"))
            return (
                f"Publishing encountered an error and has been paused (PARTIAL). "
                f"Re-invoke publisher with batch_id={batch_id} to resume. "
                f"Error: {exc}"
            )
