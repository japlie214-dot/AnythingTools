"""utils/telegram/__init__.py
Atomized Telegram utilities for AnythingTools.

ARCHITECTURAL RULE: The Telegram client is a strictly passive output sink. No messages,
handshakes, or notifications may be sent proactively by the system. Telegram messaging
is EXCLUSIVELY permitted when explicitly requested by a user-initiated job
(e.g., via the `publisher` tool).
"""
from utils.telegram.types import TelegramErrorInfo, PhaseState
from utils.telegram.rate_limiter import GlobalRateLimiter
from utils.telegram.telegram_client import TelegramAPIClient
from utils.telegram.state_manager import PhaseStateManager
from utils.telegram.validator import ArticleValidator
from utils.telegram.translator import BatchTranslator
from utils.telegram.publisher import ChannelPublisher
from utils.telegram.pipeline import PublisherPipeline

__all__ = [
    "TelegramErrorInfo",
    "PhaseState",
    "GlobalRateLimiter",
    "TelegramAPIClient",
    "PhaseStateManager",
    "ArticleValidator",
    "BatchTranslator",
    "ChannelPublisher",
    "PublisherPipeline",
]
