# clients/llm/types.py
"""All dataclasses, the abstract provider base, and MIME constants."""

import abc
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

# MIME type mapping for file extensions
MIME_TYPE_MAP: dict[str, str] = {
    ".pdf":  "application/pdf",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".bmp":  "image/bmp",
    ".tiff": "image/tiff",
}

# Set of image MIME types that use input_image blocks in the Responses API
_RESPONSES_IMAGE_MIMES: frozenset[str] = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/bmp", "image/tiff",
})


@dataclass(frozen=True)
class LLMRequest:
    """
    Unified LLM request dataclass.

    file_attachments — complete_chat ONLY. Must not be set by stream_chat callers.
    Each entry: {"history_index": int, "path": str, "mime_type": str, "base64": str}
    """
    messages: List[Dict[str, Any]]
    model: Optional[str] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    min_tokens: Optional[int] = None
    reasoning_effort: str = "medium"
    timeout_s: Optional[float] = None
    response_format: Optional[Dict[str, Any]] = None
    file_attachments: Optional[List[Dict[str, Any]]] = None  # complete_chat only


@dataclass(frozen=True)
class LLMChunk:
    text: str
    provider: str
    model: str


@dataclass(frozen=True)
class LLMResponse:
    content: str
    provider: str
    model: str
    finish_reason: Optional[str]
    usage: Dict[str, int]
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)


class LLMProvider(abc.ABC):
    @abc.abstractmethod
    async def complete_chat(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError
