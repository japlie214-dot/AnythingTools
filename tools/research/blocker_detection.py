# tools/research/blocker_detection.py
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


def is_hard_blocked(html_content: str) -> bool:
    if not html_content:
        return True
    lower_html = html_content.lower()
    if len(html_content.strip()) < 1000:
        return True
    blockers = [
        "just a moment",
        "verify you are human",
        "cloudflare",
        "ddos protection",
        "access denied",
        "checking your browser",
        "please wait",
        "security check",
        "enable javascript",
        "turn off ad blocker",
        "cookie consent",
        "access error",        # FT 403 pattern
        "potential misuse",    # FT 403 pattern
        "access blocked",      # FT 403 pattern
        "status code: 403",    # FT 403 pattern
        "<debug-panel>",       # FT 403 debug-panel pattern
    ]
    found_blockers = [b for b in blockers if b in lower_html]
    if found_blockers:
        log.dual_log(
            tag="Research:Scraper",
            message=f"Detected blockers: {found_blockers}",
            level="DEBUG",
            payload={"found_blockers": found_blockers},
        )
        return True
    return False
