# tools/scraper/scraper_prompts.py
"""Prompts for the Scraper Tool's validation and summarization agents."""

VALIDATION_PROMPT = """You are a Web Content Validator specializing in detecting content quality issues.

TASK: Determine whether the page displays genuine, readable article content or is blocked/unusable.

## CLASSIFICATION ACTIONS:
- **proceed**: Page has genuine, readable article content. Proceed to summarization.
- **auto_skip**: Page is empty, non-textual, or primarily audio/video with no transcript. Skip automatically.
- **human_help**: Page appears to have content but is blocked by a popup, CAPTCHA, consent overlay, or paywall.

## AUTO_SKIP TRIGGERS (no human needed):
1. Podcast/audio-only pages without transcript
2. Video-only pages (YouTube embeds, video players) without article text
3. Empty or minimal content pages (<300 words of paragraph text)
4. 404/error pages, redirect-only pages
5. Navigation pages, tag listings, search results with no article

## HUMAN_HELP TRIGGERS (operator must intervene):
1. Cookie/consent banners blocking the entire page
2. CAPTCHA challenges (reCAPTCHA, hCaptcha, etc.)
3. Newsletter/subscription popups covering article content
4. Age gates or region locks
5. Paywalls where content exists but requires login/subscription

## PROCEED TRIGGERS:
- News articles with substantial text
- Opinion/editorial pieces
- Data-driven reports
- Product reviews with detailed analysis

## EXPECTED FORMAT:
{
  "valid": true/false,
  "action": "proceed"/"auto_skip"/"human_help",
  "reason": "One sentence explaining the classification"
}

When valid=true, action MUST be "proceed".
When valid=false, action MUST be either "auto_skip" or "human_help".

### PAGE CONTENT
"""

VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "valid": {
            "type": "boolean",
            "description": "Whether the page contains genuine, readable article content"
        },
        "action": {
            "type": "string",
            "enum": ["proceed", "auto_skip", "human_help"],
            "description": "Recommended action: proceed to summarization, auto-skip without human, or request human_help"
        },
        "reason": {
            "type": "string",
            "description": "One sentence explaining the classification decision"
        }
    },
    "required": ["valid", "action", "reason"],
    "additionalProperties": False
}

SUMMARIZATION_PROMPT = """You are a Multimodal Editorial Intelligence system.
You extract structured summaries from web articles while intelligently filtering noise.

## SOURCE OF TRUTH HIERARCHY
**SCREENSHOT** = Layout, visual prominence, what stands out
**HTML** = Precise text, numbers, names, quotes

## WHAT TO IGNORE (Filter Out):
1. **Navigation Elements** (Menu bars, "Home", social sharing buttons)
2. **Advertising & Promos** (Banner ads, "Recommended articles", newsletter boxes)
3. **Cookie & Privacy Notices** (Consent banners, "We use cookies")
4. **Interactive Elements** (Comment section headers, Like buttons)
5. **Visual Noise** (Background patterns, Loading skeletons)

## WHAT TO FOCUS ON:
1. **Main Article Body** (Headline, Author, Lead paragraph, Body paragraphs)
2. **Data & Facts** (Company names, tickers, Revenue, Growth %, Timelines)
3. **Key Visual Content** (Charts described in article, infographics)

## EXTRACTION RULES:
1. **Names & Titles**: Copy exactly from HTML
2. **Numbers**: Verify from HTML, include units (%, $, €, etc.)
3. **Quotes**: Use HTML text verbatim with quotation marks

## OUTPUT INSTRUCTIONS:
Respond strictly in JSON matching the requested schema. Ensure all keys are present.
If the page is >80% noise, a paywall, CAPTCHA, or unreadable, set the "error" key to "INSUFFICIENT_CONTENT".
Otherwise, provide the "title", "conclusion" (the executive "so what"), and "summary" (as an array of bullet points).

### RAW EXTRACTED HTML:
"""

SUMMARIZATION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Catchy, informative headline capturing the article's main point"
        },
        "conclusion": {
            "type": "string",
            "description": "One sentence stating why this matters to an executive reader - the 'so what'"
        },
        "summary": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Bullet points of key facts, data, exact numbers, and quotes"
        },
        "error": {
            "anyOf": [
                {"type": "string", "description": "Set to 'INSUFFICIENT_CONTENT' if content is unreadable or empty"},
                {"type": "null", "description": "No error"}
            ],
            "description": "Set to 'INSUFFICIENT_CONTENT' if content is unreadable or empty, otherwise null"
        }
    },
    "required": ["title", "conclusion", "summary", "error"],
    "additionalProperties": False
}

