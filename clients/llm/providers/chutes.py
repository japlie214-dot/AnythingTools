# clients/llm/providers/chutes.py
"""ChutesProvider class and its private usage helper."""

import asyncio
from typing import Any, Dict

from openai import AsyncOpenAI, RateLimitError, APITimeoutError, APIConnectionError

import config
from clients.llm.types import LLMRequest, LLMResponse, LLMChunk, LLMProvider
from clients.llm.utils import (
    _build_timeout, _with_retry,
    MAX_API_RETRIES, BASE_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS,
)
from clients.llm.payloads import _apply_common_payload
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


def _extract_usage(response: Any) -> Dict[str, int]:
    usage              = getattr(response, "usage", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    reasoning_tokens   = (
        getattr(completion_details, "reasoning_tokens", 0) if completion_details else 0
    )
    return {
        "prompt_tokens":     getattr(usage, "prompt_tokens",     0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "reasoning_tokens":  reasoning_tokens or 0,
        "total_tokens":      getattr(usage, "total_tokens",      0) or 0,
    }


class ChutesProvider(LLMProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=config.CHUTES_KEY,
            base_url="https://api.chutes.ai/v1",
            timeout=_build_timeout(),
        )
        self.provider_name = "chutes"
        self.default_model = getattr(
            config, "CHUTES_MODEL", "meta-llama/Llama-3.3-70B-Instruct"
        )

    def _build_payload(self, request: LLMRequest) -> Dict[str, Any]:
        payload = {
            "model":         request.model or self.default_model,
            "messages":      request.messages,
            "extra_headers": {"X-Enable-Thinking": "true"},
        }
        if request.min_tokens is not None:
            payload["extra_body"] = {"min_tokens": request.min_tokens}
        return _apply_common_payload(payload, request)

    async def complete_chat(self, request: LLMRequest) -> LLMResponse:
        _resolved_model = request.model or self.default_model
        log.dual_log(
            tag="LLM:Chutes:Request",
            message=f"Sending request to {_resolved_model}",
            payload={
                "model":    _resolved_model,
                "messages": request.messages,
                "tools":    request.tools,
            },
        )
        payload  = self._build_payload(request)
        response = await _with_retry(
            lambda: self.client.chat.completions.create(**payload)
        )
        _content    = ""
        _tool_calls: list[dict] = []
        choice      = None
        try:
            choice = response.choices[0]
            msg    = getattr(choice, "message", None)
            if not msg and isinstance(choice, dict):
                msg = choice.get("message")
            if msg:
                _content = (
                    getattr(msg, "content", None)
                    or (msg.get("content") if isinstance(msg, dict) else "")
                    or ""
                )
                tool_calls_raw = (
                    getattr(msg, "tool_calls", None)
                    or (msg.get("tool_calls") if isinstance(msg, dict) else None)
                )
                if tool_calls_raw:
                    for tc in tool_calls_raw:
                        if isinstance(tc, dict):
                            name      = tc.get("name", "")
                            arguments = tc.get("arguments", "")
                            call_id   = tc.get("id", "")
                        else:
                            name      = getattr(tc, "name",      "")
                            arguments = getattr(tc, "arguments", "")
                            call_id   = getattr(tc, "id",        "")
                        _tool_calls.append({
                            "id": call_id or "", "type": "function",
                            "function": {"name": name or "", "arguments": arguments or ""},
                        })
            else:
                _content = (
                    getattr(choice, "text", None)
                    or (choice.get("text") if isinstance(choice, dict) else "")
                    or ""
                )
        except Exception:
            pass

        _final_model     = getattr(response, "model", None) or _resolved_model
        tool_names       = list(dict.fromkeys(
            tc.get("function", {}).get("name")
            for tc in _tool_calls
            if tc.get("function", {}).get("name")
        ))
        tools_called_str = (
            f"[Tools: {', '.join(tool_names)}]" if tool_names else "[Direct Reply]"
        )
        log.dual_log(
            tag="LLM:Chutes:Response",
            message=f"Received response. {tools_called_str}",
            payload={
                "model":         _final_model,
                "finish_reason": getattr(choice, "finish_reason", None),
                "usage":         _extract_usage(response),
                "content":       _content,
                "tool_calls":    _tool_calls,
                "tools_called":  tool_names,
            },
        )
        return LLMResponse(
            content=_content,
            provider=self.provider_name,
            model=_final_model,
            finish_reason=getattr(choice, "finish_reason", None),
            usage=_extract_usage(response),
            tool_calls=_tool_calls,
        )
