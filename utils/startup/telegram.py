# utils/startup/telegram.py

import asyncio
import config
from api.telegram_client import TelegramBot
from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)

async def start_telegram_handshake() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        log.dual_log(tag="Startup:Telegram", message="No TELEGRAM_BOT_TOKEN found, skipping handshake.", level="INFO")
        return

    asyncio.create_task(TelegramBot.run_orphan_handshake())
    log.dual_log(tag="Startup:Telegram", message="Telegram orphan handshake task launched.", level="INFO")
