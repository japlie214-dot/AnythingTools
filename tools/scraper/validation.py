# tools/scraper/validation.py
"""Article validation: LLM-based quality assessment and HITL escalation."""

from bs4 import BeautifulSoup
from tools.scraper.hitl import ValidationAction, _hitl_state
from tools.scraper.scraper_prompts import VALIDATION_PROMPT, VALIDATION_SCHEMA
from utils.text_processing import parse_llm_json, escape_prompt_separators
from tools.scraper.browser import _build_multimodal_messages
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def check_video_audio_skip(raw_html: str, url: str) -> dict | None:
    soup = BeautifulSoup(raw_html or "", "html.parser")
    video_audio_tags = soup.find_all(["video", "audio"])
    iframe_embeds = soup.find_all("iframe", src=True)
    video_platforms = ["youtube.com/embed", "youtu.be", "vimeo.com", "dailymotion.com"]
    has_video_embed = any(
        any(platform in (tag.get("src") or "") for platform in video_platforms)
        for tag in iframe_embeds
    )
    if video_audio_tags or has_video_embed:
        paragraph_text = " ".join(p.get_text(strip=True) for p in soup.find_all("p"))
        if len(paragraph_text) < 500:
            log.dual_log(tag="Scraper:Validation:AutoSkip", message="Page auto-skipped: video/audio primary content", level="WARNING", payload={"url": url})
            return {"status": "SKIPPED", "reason": "Auto-skipped: Page primary content is video/audio with insufficient article text."}
    return None

def check_paywall(raw_html: str, url: str, job_id: str | None, cancellation_flag) -> str | None:
    from tools.scraper.paywall import PaywallDetector
    pw_result = PaywallDetector().detect(raw_html)
    if pw_result.is_paywalled:
        log.dual_log(tag="Scraper:Validation:Paywall", message=f"{pw_result.blocker_type.title()} detected", level="WARNING", payload={"url": url, "type": pw_result.blocker_type})
        decision = _hitl_state.request_decision(job_id, url, f"BLOCKED PAGE - Action Required: {pw_result.blocker_type.title()} detected (Indicators: {pw_result.detected_indicators})")
        if decision == "cancel" and cancellation_flag is not None:
            cancellation_flag.set()
        return decision
    return None

def validate_article(raw_html: str, b64_image: str | None, url: str, sync_llm_chat) -> tuple[ValidationAction, dict | None, str | None]:
    slim_val  = raw_html[:15000]
    val_msgs  = _build_multimodal_messages(
        VALIDATION_PROMPT,
        escape_prompt_separators(slim_val) + "\n###",
        b64_image,
    )
    try:
        val_resp = sync_llm_chat(
            val_msgs,
            response_format={"type": "json_schema", "json_schema": {"name": "validation", "strict": True, "schema": VALIDATION_SCHEMA}},
        )
        used_format = "json_schema"
    except Exception as format_exc:
        from openai import BadRequestError
        if isinstance(format_exc, BadRequestError):
            log.dual_log(tag="Scraper:Validation:Fallback", message="json_schema format rejected; falling back to json_object", level="WARNING", payload={"error": str(format_exc)})
            val_resp = sync_llm_chat(val_msgs, response_format={"type": "json_object"})
            used_format = "json_object"
        else:
            raise

    val_data  = parse_llm_json(val_resp.content or "{}")
    log.dual_log(tag="Scraper:Validation:Response", message="LLM validation response received", payload={"url": url, "raw": val_resp.content, "parsed": val_data, "format": used_format})
    if not val_data.get("valid", False):
        reason = val_data.get("reason", "unknown")
        action_str = val_data.get("action", "human_help")
        try:
            action = ValidationAction(action_str)
        except ValueError:
            log.dual_log(tag="Scraper:Validation:Error", message=f"Unknown action '{action_str}', defaulting to human_help", level="WARNING", payload={"url": url})
            action = ValidationAction.HUMAN_HELP
            
        hitl_reason = val_data.get("hitl_reason")
        if hitl_reason:
            hitl_reason = str(hitl_reason)[:100]
            
        return action, val_data, hitl_reason

    return ValidationAction.PROCEED, None, None
