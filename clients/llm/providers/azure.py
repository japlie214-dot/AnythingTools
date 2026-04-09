# clients/llm/providers/azure.py
"""AzureProvider class and all Azure Responses API helpers."""

import asyncio
from typing import Any, Dict, AsyncGenerator

from openai import AsyncAzureOpenAI, RateLimitError, APITimeoutError, APIConnectionError

import config
from clients.llm.types import LLMRequest, LLMResponse, LLMChunk, LLMProvider
from clients.llm.utils import (
    _build_timeout, _with_retry,
    MAX_API_RETRIES, BASE_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS,
)
from clients.llm.payloads import _apply_common_payload, _build_responses_payload
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


def _extract_responses_usage(response: Any) -> Dict[str, int]:
    """Parse token counts from a responses.create Response object."""
    usage          = getattr(response, "usage", None)
    output_details = getattr(usage, "output_tokens_details", None)
    reasoning_tokens = (
        getattr(output_details, "reasoning_tokens", 0) if output_details else 0
    )
    return {
        "prompt_tokens":     getattr(usage, "input_tokens",  0) or 0,
        "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
        "reasoning_tokens":  reasoning_tokens or 0,
        "total_tokens":      getattr(usage, "total_tokens",  0) or 0,
    }


def _extract_responses_content_and_tools(
    response: Any,
) -> tuple[str, list[Dict[str, Any]]]:
    """Extract text content and tool calls from a responses.create Response object."""
    content_parts: list[str]         = []
    tool_calls:    list[Dict[str, Any]] = []
    for item in (getattr(response, "output", None) or []):
        item_type = getattr(item, "type", "")
        if item_type == "message":
            for part in (getattr(item, "content", None) or []):
                if getattr(part, "type", "") == "output_text":
                    content_parts.append(getattr(part, "text", ""))
        elif item_type == "function_call":
            tool_calls.append({
                "id":   getattr(item, "call_id", ""),
                "type": "function",
                "function": {
                    "name":      getattr(item, "name",      ""),
                    "arguments": getattr(item, "arguments", ""),
                },
            })
    return "".join(content_parts), tool_calls


def _dump_tool_call(tool_call: Any) -> Dict[str, Any]:
    if hasattr(tool_call, "model_dump"):
        return tool_call.model_dump()
    return tool_call.dict()


class AzureProvider(LLMProvider):
    def __init__(self):
        self.client = AsyncAzureOpenAI(
            api_key=config.AZURE_KEY,
            azure_endpoint=config.AZURE_ENDPOINT,
            api_version="2025-03-01-preview",
            timeout=_build_timeout(),
        )
        self.provider_name = "azure"
        self.default_model = getattr(config, "AZURE_DEPLOYMENT", "gpt-5.4-mini")

    def _build_payload(self, request: LLMRequest, *, stream: bool) -> Dict[str, Any]:
        payload = {
            "model":    request.model or self.default_model,
            "messages": request.messages,
            "stream":   stream,
        }
        if request.reasoning_effort and any(
            m in (request.model or self.default_model) for m in ["o1", "o3", "gpt-5"]
        ):
            payload["reasoning_effort"] = request.reasoning_effort
        return _apply_common_payload(payload, request)

    async def stream_chat(
        self, request: LLMRequest
    ) -> AsyncGenerator[LLMChunk, None]:
        for attempt in range(1, MAX_API_RETRIES + 1):
            try:
                response = await self.client.chat.completions.create(
                    **self._build_payload(request, stream=True)
                )
                async for chunk in response:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    text  = delta.content if delta and getattr(delta, "content", None) else ""
                    if text:
                        yield LLMChunk(
                            text=text,
                            provider=self.provider_name,
                            model=request.model or self.default_model,
                        )
                return
            except (RateLimitError, APITimeoutError, APIConnectionError):
                if attempt == MAX_API_RETRIES:
                    raise
                await asyncio.sleep(
                    min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                )

    async def complete_chat(self, request: LLMRequest) -> LLMResponse:
        _resolved_model = request.model or self.default_model
        log.dual_log(
            tag="LLM:Azure:Request",
            message=f"Sending request to {_resolved_model}",
            payload={
                "model":            _resolved_model,
                "messages":         request.messages,
                "tools":            request.tools,
                "file_attachments": [
                    att["path"] for att in (request.file_attachments or [])
                ],
            },
        )
        payload  = _build_responses_payload(request, self.default_model)
        response = await _with_retry(
            lambda: self.client.responses.create(
                **payload, extra_headers={"X-Enable-Thinking": "true"}
            )
        )
        _content, _tool_calls = _extract_responses_content_and_tools(response)
        _final_model          = getattr(response, "model", None) or _resolved_model

        # Phase 5: deduplicated tool-name log (dict.fromkeys preserves order)
        tool_names       = list(dict.fromkeys(
            tc.get("function", {}).get("name")
            for tc in _tool_calls
            if tc.get("function", {}).get("name")
        ))
        tools_called_str = (
            f"[Tools: {', '.join(tool_names)}]" if tool_names else "[Direct Reply]"
        )
        log.dual_log(
            tag="LLM:Azure:Response",
            message=f"Received response. {tools_called_str}",
            payload={
                "model":         _final_model,
                "finish_reason": getattr(response, "status", None),
                "usage":         _extract_responses_usage(response),
                "content":       _content,
                "tool_calls":    _tool_calls,
                "tools_called":  tool_names,
            },
        )
        return LLMResponse(
            content=_content,
            provider=self.provider_name,
            model=_final_model,
            finish_reason=getattr(response, "status", None),
            usage=_extract_responses_usage(response),
            tool_calls=_tool_calls,
        )
