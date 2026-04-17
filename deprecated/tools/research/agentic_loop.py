# deprecated/tools/research/agentic_loop.py
# MAINTENANCE WARNING: agentic_recovery_loop() calls asyncio.run(_run_steps())
# internally. This module MUST only ever be invoked from a synchronous Botasaurus
# @browser-decorated thread. Calling it from an active async context (e.g., an
# async test harness or a future async tool dispatcher) will immediately raise:
#   RuntimeError: This event loop is already running.

import json
import asyncio

from botasaurus.browser import Driver

from clients.llm import UnifiedLLM, LLMRequest
from tools.research.research_prompts import SCRAPER_RECOVERY_PROMPT
from utils.browser_utils import safe_google_get
from utils.logger import get_dual_logger
from utils.som_utils import reinject_all, verify_visibility_and_click, verify_visibility_and_type, extract_surgical_html
from utils.text_processing import clean_html_for_agent
from utils.vision_utils import capture_and_optimize
from tools.research.blocker_detection import is_hard_blocked
from utils.hitl import pause_for_hitl

log = get_dual_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# BOTASAURUS INTERACTION POLICY — READ BEFORE ADDING ANY BROWSER ACTION
# ═══════════════════════════════════════════════════════════════════════════
# ALL click / scroll / type / navigate actions MUST use Botasaurus native API.
# See PLAN-02 or the Botasaurus README for the full method reference.
# run_js() is ONLY permitted for DOM mutations that have no native equivalent:
#   data-ai-id injection, badge overlay management, MutationObserver,
#   and getBoundingClientRect/elementFromPoint occlusion tests.
# If you reach for run_js() for a click, scroll, or type — STOP.
# ═══════════════════════════════════════════════════════════════════════════
#
# The run_js() calls present in this file are ALL legitimate exceptions:
#   • Badge removal  querySelectorAll('[data-ai-badge]').forEach(b=>b.remove())
#     → no Botasaurus bulk-remove API exists.
# Every other interaction (visit_url, verified_click, verified_type) is
# already routed through Botasaurus native methods in som_utils.py.
# ═══════════════════════════════════════════════════════════════════════════


def _get_dom_tools() -> list:
    return [
        {
            "type": "function",
            "name": "verified_type",
            "description": "Type text into an input field identified by its data-ai-id number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ai_id": {"type": "integer", "description": "The data-ai-id value of the target input field."},
                    "text":  {"type": "string",  "description": "The exact text to type into the field."},
                },
                "required": ["ai_id", "text"],
            },
        },
        {
            "type": "function",
            "name": "verified_click",
            "description": "Click an interactive element identified by its data-ai-id number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ai_id": {"type": "integer", "description": "The data-ai-id value of the target element."},
                },
                "required": ["ai_id"],
            },
        },
        {
            "type": "function",
            "name": "visit_url",
            "description": "Navigate the browser to a new URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
        {
            "type": "function",
            "name": "declare_success",
            "description": "Call this when the main article content is fully visible and unobstructed.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
        {
            "type": "function",
            "name": "human_help",
            "description": (
                "Call this when blocked by a hard CAPTCHA (Cloudflare Turnstile, "
                "image-grid reCAPTCHA), or when the HTML dump is truncated."
            ),
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    ]


def agentic_recovery_loop(
    driver: Driver,
    topic_hint: str,
    max_steps: int = 3,
    cancellation_flag=None,
    headless: bool = False,
) -> bool:
    # id_tracking is initialised here in the synchronous outer scope so it
    # persists across all step iterations via closure — consistent with the
    # original implementation.
    id_tracking: dict = {}

    async def _run_steps() -> bool:
        # Bypass the singleton cache so the httpx.AsyncClient binds to THIS
        # event loop rather than the one that was active at import time.
        llm = UnifiedLLM(provider_type="azure")
        _HTML_TOKEN_CEILING_CHARS = 320_000
        _HTML_TRUNCATION_MARKER = (
            "\n...[HTML TRUNCATED — call human_help to scroll to the "
            "relevant section before acting]"
        )

        for step in range(max_steps):
            reinject_all(driver, id_tracking)
            img_infos = capture_and_optimize(driver, step)

            # Strip badges from main document before HTML extraction so badge
            # text does not pollute the DOM dump; data-ai-id attributes are unaffected.
            driver.run_js(
                "document.querySelectorAll('[data-ai-badge]')"
                ".forEach(function(b){b.remove();})"
            )
            # Strip badges from all registered iframe contexts.
            for loc_key in id_tracking:
                if loc_key == "main":
                    continue
                loc_type, loc_val = loc_key.split(":", 1)
                try:
                    ctx = (
                        driver.select_iframe(loc_val)
                        if loc_type == "css"
                        else driver.get_iframe_by_link(loc_val)
                    )
                    ctx.run_js(
                        "document.querySelectorAll('[data-ai-badge]')"
                        ".forEach(function(b){b.remove();})"
                    )
                except Exception:
                    pass  # stale iframe after reinject_all; skip silently

            raw_html = extract_surgical_html(driver)
            slim_html = clean_html_for_agent(
                raw_html,
                max_chars=_HTML_TOKEN_CEILING_CHARS,
                extra_attrs={"data-ai-id"},
            )
            if slim_html.endswith("... [TRUNCATED]"):
                cutoff = slim_html.rfind(">", 0, _HTML_TOKEN_CEILING_CHARS)
                cutoff = cutoff if cutoff > 0 else _HTML_TOKEN_CEILING_CHARS
                slim_html = slim_html[: cutoff + 1] + _HTML_TRUNCATION_MARKER

            user_text = f"Step {step + 1}/{max_steps}.\nCurrent DOM:\n{slim_html}"
            if _HTML_TRUNCATION_MARKER.strip() in slim_html:
                user_text += (
                    "\n\n⚠ HTML is truncated. "
                    "You must call human_help before acting on HTML content."
                )

            user_content: list = [{"type": "text", "text": user_text}]
            has_image = False
            for _info in img_infos:
                if _info.get("b64") and _info.get("status") == "OK":
                    user_content.append({
                        "type":      "image_url",
                        "image_url": {"url": f"data:{_info['mime']};base64,{_info['b64']}"},
                    })
                    has_image = True
                elif _info.get("status") == "EMPTY_SLICE_DISCARDED":
                    user_content.append({
                        "type": "text",
                        "text": "[System: Image slice omitted — blank or low-content region.]",
                    })
                elif _info.get("status") in ("IMAGE_TOO_LARGE_SKIPPED",
                                              "Screenshot Analysis Unavailable"):
                    user_content.append({
                        "type": "text",
                        "text": "[System: Image slice unavailable — proceed using HTML only.]",
                    })
            if not has_image:
                user_content[0]["text"] += "\n[Screenshot unavailable — use HTML only]"

            system_prompt = SCRAPER_RECOVERY_PROMPT.format(topic_hint=topic_hint)
            response = await llm.complete_chat(LLMRequest(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_content},
                ],
                tools=_get_dom_tools(),
            ))

            if not response.tool_calls:
                # No tool called — LLM produced text only; exit loop.
                break

            for call in response.tool_calls:
                try:
                    args_str = call["function"].get("arguments", "{}")
                    args = json.loads(args_str) if args_str else {}
                except json.JSONDecodeError:
                    args = {}

                name = call["function"]["name"]

                if name == "declare_success":
                    return True

                elif name == "human_help":
                    reason = args.get("reason", "Unknown reason")
                    if headless:
                        log.dual_log(
                            tag="Research:Scraper",
                            message=f"Headless mode — human help skipped: {reason}",
                            level="WARNING",
                        )
                    else:
                        from utils.hitl import pause_for_hitl
                        pause_for_hitl(f"[AI NAVIGATOR] HUMAN HELP REQUESTED: {reason}")
                        return False

                elif name == "verified_click":
                    ai_id = args.get("ai_id")
                    result = verify_visibility_and_click(driver, ai_id, id_tracking)
                    if result == "SUCCESS":
                        reinject_all(driver, id_tracking)
                    else:
                        log.dual_log(
                            tag="Research:Scraper",
                            message=f"verified_click returned non-SUCCESS: {result}",
                            level="WARNING",
                            payload={"ai_id": ai_id, "result": result},
                        )

                elif name == "verified_type":
                    ai_id = args.get("ai_id")
                    text  = args.get("text", "")
                    result = verify_visibility_and_type(driver, ai_id, text, id_tracking)
                    if result == "SUCCESS":
                        reinject_all(driver, id_tracking)
                    else:
                        log.dual_log(
                            tag="Research:Scraper",
                            message=f"verified_type returned non-SUCCESS: {result}",
                            level="WARNING",
                            payload={"ai_id": ai_id, "result": result},
                        )

                elif name == "visit_url":
                    url = args.get("url", "")
                    if url:
                        safe_google_get(driver, url)
                        reinject_all(driver, id_tracking)
                    else:
                        log.dual_log(
                            tag="Research:Scraper",
                            message="visit_url called with empty URL — skipped.",
                            level="WARNING",
                        )

        return not is_hard_blocked(extract_surgical_html(driver))

    return asyncio.run(_run_steps())
