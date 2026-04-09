# clients/llm/__init__.py
"""Public contract re-export.

Every symbol previously importable from clients.llm_client is importable from
clients.llm with identical identity.
"""

from clients.llm.types import (
    LLMRequest, LLMChunk, LLMResponse, LLMProvider,
    MIME_TYPE_MAP, _RESPONSES_IMAGE_MIMES,
)
from clients.llm.utils import (
    DEFAULT_CONNECT_TIMEOUT_S, DEFAULT_READ_TIMEOUT_S,
    DEFAULT_WRITE_TIMEOUT_S, DEFAULT_POOL_TIMEOUT_S,
    MAX_API_RETRIES, BASE_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS,
    _build_timeout, _with_retry,
    is_context_length_error,
)
from clients.llm.payloads import _apply_common_payload, _build_responses_payload
from clients.llm.factory import get_llm_client, UnifiedLLM

__all__ = [
    # Types
    "LLMRequest",
    "LLMChunk",
    "LLMResponse",
    "LLMProvider",
    "MIME_TYPE_MAP",
    "_RESPONSES_IMAGE_MIMES",
    # Utils
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_READ_TIMEOUT_S",
    "DEFAULT_WRITE_TIMEOUT_S",
    "DEFAULT_POOL_TIMEOUT_S",
    "MAX_API_RETRIES",
    "BASE_BACKOFF_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "_build_timeout",
    "_with_retry",
    "is_context_length_error",
    # Payloads
    "_apply_common_payload",
    "_build_responses_payload",
    # Factory
    "get_llm_client",
    "UnifiedLLM",
]
