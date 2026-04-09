# clients/llm/payloads.py
"""Payload builders for chat.completions and Responses API paths."""

import os
from typing import Dict, Any, List

import config
from clients.llm.types import LLMRequest, _RESPONSES_IMAGE_MIMES


# ── Module-level path constants (computed once at import time) ─────────────────
# Used for image-origin classification in _build_responses_payload().
# Both are absolute so os.path.commonpath() comparisons are always consistent.
_SCREENSHOT_DIR_ABS: str = os.path.abspath(
    getattr(config, "BROWSER_SCREENSHOT_DIR", "data/temp/browser_state")
)
# Mirrors the temp_dir value hardcoded in bot/handlers.py::handle_document().
# If that constant is ever promoted to config, update this reference to match.
_UPLOAD_DIR_ABS: str = os.path.abspath(os.path.join("data", "temp", "multimodal"))


def _apply_common_payload(
    payload: Dict[str, Any], request: "LLMRequest"
) -> Dict[str, Any]:
    """
    Apply common payload enhancements for both chat.completions and Responses API paths.
    
    Handles:
    - Tool configuration
    - Reasoning model detection
    - Temperature/max_tokens/timeout
    - Response format (JSON object/schema)
    - Multimodal message validation
    """
    # Tools and tool choice
    if request.tools is not None:
        payload["tools"] = request.tools
    if request.tool_choice is not None:
        payload["tool_choice"] = request.tool_choice

    # Reasoning model detection
    resolved_model = payload.get("model", "")
    is_reasoning = any(m in resolved_model for m in ["o1", "o3", "gpt-5"])

    # Temperature (not used in reasoning models)
    if request.temperature is not None and not is_reasoning:
        payload["temperature"] = request.temperature
    
    # Max tokens
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    
    # Timeout
    if request.timeout_s is not None:
        payload["timeout"] = request.timeout_s

    # Response format handling
    if request.response_format is not None:
        payload["response_format"] = request.response_format
        if request.response_format.get("type") == "json_object":
            has_json = False
            for msg in payload.get("messages", []):
                content = msg.get("content", "")
                if isinstance(content, str) and "json" in content.lower():
                    has_json = True
                    break
                elif isinstance(content, list):
                    for part in content:
                        if (
                            isinstance(part, dict)
                            and part.get("type") == "text"
                            and "json" in part.get("text", "").lower()
                        ):
                            has_json = True
                            break
            if not has_json and payload.get("messages"):
                new_msgs = list(payload["messages"])
                target = dict(new_msgs[-1])
                c = target.get("content", "")
                if isinstance(c, str):
                    target["content"] = c + "\n\nOutput strictly in JSON."
                elif isinstance(c, list):
                    new_c = list(c)
                    new_c.append({"type": "text", "text": "\n\nOutput strictly in JSON."})
                    target["content"] = new_c
                new_msgs[-1] = target
                payload["messages"] = new_msgs

    # Enhanced multimodal support: Validate message format
    if "messages" in payload and isinstance(payload["messages"], list):
        for msg in payload["messages"]:
            # Ensure role is present
            if "role" not in msg:
                msg["role"] = "user"

            # Validate content structure
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "image_url":
                        image_url = item.get("image_url")
                        if isinstance(image_url, str):
                            item["image_url"] = {"url": image_url}
                        elif isinstance(image_url, dict) and "url" in image_url:
                            pass  # Already correct
                        else:
                            raise ValueError(f"Invalid image_url format: {image_url}")

    return payload


def _classify_image_prompt(att_path: str) -> str:
    """Return the appropriate synthetic-user framing text for a single image path.
    Uses os.path.commonpath() so the comparison is correct on both Windows and Linux
    regardless of trailing separators.
    Type A — browser state screenshot (path is inside _SCREENSHOT_DIR_ABS).
    Type C — user upload            (path is inside _UPLOAD_DIR_ABS).
    Type B — tool artifact           (all other origins).
    """
    abs_path = os.path.abspath(att_path)
    try:
        if os.path.commonpath([abs_path, _SCREENSHOT_DIR_ABS]) == _SCREENSHOT_DIR_ABS:
            return (
                "System: Visual confirmation of the current browser state captured "
                "immediately after the previous tool action. Interpret this image as "
                "evidence of the browser's present state, not a user submission."
            )
    except ValueError:
        pass  # different drives on Windows — not a screenshot path
    try:
        if os.path.commonpath([abs_path, _UPLOAD_DIR_ABS]) == _UPLOAD_DIR_ABS:
            return "System: Image submitted directly by the user."
    except ValueError:
        pass
    return "System: Visual artifact produced as the output of a completed tool action."


def _build_responses_payload(
    request: "LLMRequest", default_model: str
) -> Dict[str, Any]:
    """
    Build a payload dict for client.responses.create.

    Structural rules enforced here:
    • role=system → excluded from `input` (handled separately)
    • Each non-system message is translated to an input array entry.
    • If a file_attachment carries history_index == i (0-based over non-system messages),
      its block is appended to that message's content list.
    • PDFs/documents  → type=input_file  with file_data="data:<mime>;base64,<b64>"
    • Images          → type=input_image with image_url="data:<mime>;base64,<b64>"
    • response_format → mapped to text.format parameter.
    • Role-flipping for assistant turns with image attachments (Phase 2).
    """
    resolved_model = request.model or default_model
    payload: Dict[str, Any] = {"model": resolved_model, "input": []}

    # ── (a) Extract and separate system messages ────────────────────────────
    non_system_messages: List[Dict[str, Any]] = []
    system_instructions: List[str] = []

    for msg in request.messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str):
                system_instructions.append(content)
            else:
                # Handle list content
                text_parts = []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        text_parts.append(c.get("text", ""))
                system_instructions.append(" ".join(text_parts))
        else:
            non_system_messages.append(msg)

    if system_instructions:
        payload["instructions"] = "\n".join(system_instructions)

    # Build a lookup: history_index → list of file attachment dicts
    attachment_by_index: Dict[int, List[Dict[str, Any]]] = {}
    for att in (request.file_attachments or []):
        idx = att.get("history_index", -1)
        attachment_by_index.setdefault(idx, []).append(att)

    # ── (b) Build input array, co-locating attachments ───────────────────────
    for i, msg in enumerate(non_system_messages):
        role = msg.get("role")

        # ── (1) Map 'tool' role to 'function_call_output' item type ────────
        if role == "tool":
            payload["input"].append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id"),
                "output": msg.get("content"),
            })
            continue

        raw_content = msg.get("content", "")
        text_str: str = (
            raw_content if isinstance(raw_content, str)
            else " ".join(
                c.get("text", "") for c in raw_content
                if isinstance(c, dict) and c.get("type") == "text"
            )
        )

        atts_for_msg = attachment_by_index.get(i, [])
        tool_calls = msg.get("tool_calls")

        # ── Phase 2: Role-flip for assistant turns carrying images ────────
        has_image = (
            role == "assistant"
            and any(a["mime_type"] in _RESPONSES_IMAGE_MIMES for a in atts_for_msg)
        )
        
        if has_image:
            # 1. Assistant text-reasoning item (no images)
            if text_str:
                payload["input"].append({
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text_str}],
                })

            # 2. Synthetic user item carrying images and context prompt
            #    Prompt type is determined by the first attachment's origin path.
            prompt_text = _classify_image_prompt(atts_for_msg[0]["path"])
            synth_parts: List[Dict[str, Any]] = [
                {"type": "input_text", "text": prompt_text}
            ]
            for att in atts_for_msg:
                mime = att["mime_type"]
                b64 = att["base64"]
                fname = os.path.basename(att["path"])
                if mime in _RESPONSES_IMAGE_MIMES:
                    synth_parts.append({
                        "type": "input_image",
                        "image_url": f"data:{mime};base64,{b64}",
                    })
                else:
                    synth_parts.append({
                        "type": "input_file",
                        "filename": fname,
                        "file_data": f"data:{mime};base64,{b64}",
                    })
            payload["input"].append({"role": "user", "content": synth_parts})

            # 3. function_call siblings appended AFTER the synthetic user item
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    payload["input"].append({
                        "type": "function_call",
                        "call_id": tc.get("id"),
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments"),
                    })

        else:
            # ── Standard path (user messages, or assistant turns without images) ─
            if not atts_for_msg and not tool_calls:
                payload["input"].append({"role": role, "content": text_str})
            else:
                content_parts: List[Dict[str, Any]] = []
                if text_str:
                    p_type = "output_text" if role == "assistant" else "input_text"
                    content_parts.append({"type": p_type, "text": text_str})

                # Attachments mapped as before
                for att in atts_for_msg:
                    mime: str = att["mime_type"]
                    b64: str = att["base64"]
                    fname: str = os.path.basename(att["path"])
                    if mime in _RESPONSES_IMAGE_MIMES:
                        content_parts.append({
                            "type": "input_image",
                            "image_url": f"data:{mime};base64,{b64}",
                        })
                    else:
                        content_parts.append({
                            "type": "input_file",
                            "filename": fname,
                            "file_data": f"data:{mime};base64,{b64}",
                        })

                # Append the message entry (role + content parts)
                payload["input"].append({"role": role, "content": content_parts})

            # ── (2) Map 'assistant' tool calls to sibling 'function_call' items
            if role == "assistant" and tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    payload["input"].append({
                        "type": "function_call",
                        "call_id": tc.get("id"),
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments"),
                    })

    # ── Tools ─────────────────────────────────────────────────────────────────
    if request.tools is not None:
        payload["tools"] = request.tools
    if request.tool_choice is not None:
        payload["tool_choice"] = request.tool_choice

    # ── Reasoning / temperature ───────────────────────────────────────────────
    is_reasoning = any(m in resolved_model for m in ["o1", "o3", "gpt-5"])
    if request.reasoning_effort and is_reasoning:
        payload["reasoning"] = {"effort": request.reasoning_effort}
    if request.temperature is not None and not is_reasoning:
        payload["temperature"] = request.temperature
    if request.max_tokens is not None:
        payload["max_output_tokens"] = request.max_tokens
    if request.timeout_s is not None:
        payload["timeout"] = request.timeout_s

    # ── response_format → text.format ────────────────────────────────────────
    if request.response_format is not None:
        fmt_type = request.response_format.get("type")
        if fmt_type == "json_object":
            payload["text"] = {"format": {"type": "json_object"}}
            has_json = False

            # The Azure Responses API strictly requires 'json' in the `input` array itself,
            # ignoring the `instructions` (system prompt) for this validation.
            for item in payload.get("input", []):
                c = item.get("content", "")
                if isinstance(c, str) and "json" in c.lower():
                    has_json = True
                    break
                elif isinstance(c, list):
                    for part in c:
                        if part.get("type") in ("input_text", "output_text") and "json" in part.get("text", "").lower():
                            has_json = True
                            break

            if not has_json:
                if not payload.get("input"):
                    payload["input"] = [{"role": "user", "content": "Output strictly in JSON."}]
                else:
                    # Target the last input message (most likely the user's active prompt)
                    target_input = payload["input"][-1]
                    c = target_input.get("content", "")
                    if isinstance(c, str):
                        target_input["content"] = c + "\n\nOutput strictly in JSON."
                    elif isinstance(c, list):
                        c.append({"type": "input_text", "text": "\n\nOutput strictly in JSON."})
        elif fmt_type == "json_schema":
            payload["text"] = {"format": request.response_format}

    return payload
