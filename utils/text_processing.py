# utils/text_processing.py
"""
Centralized text processing utilities for the SumAnal project.

This module provides deterministic sanitization, parsing, and transformation
utilities used by multiple components of the agentic architecture.
"""

import re
import json
import asyncio
import logging
from typing import Optional, List, Dict, Any
from bs4 import BeautifulSoup, Comment
from urllib.parse import urlparse, urlunparse

from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


def escape_prompt_separators(text: str) -> str:
    """Escape or neutralize ### sequences in dynamic content to prevent prompt injection."""
    if not text:
        return ""
    return text.replace("###", "---")


def escape_markdown_v2(text: str) -> str:
    """
    Global MarkdownV2 parser and escaper.
    Splits text by MarkdownV2 structural entities and escapes the reserved
    characters in the plaintext segments to prevent Telegram 400 errors.
    """
    if not text:
        return ""
    
    # Matches markdown entities: links, code blocks, inline code, bold, italic, strikethrough, spoiler
    pattern = re.compile(r'(\[[^\]]+\]\([^)]+\)|```[\s\S]+?```|`[^`]+`|\*[^*]+\*|_[^_]+_|~[^~]+~|\|\|[\s\S]+?\|\|)')
    parts = pattern.split(text)
    escaped_parts = []
    
    for i, part in enumerate(parts):
        if i % 2 == 0:
            # Plaintext: escape all reserved characters strictly (including backslash)
            escaped = re.sub(r'([\\_*\[\]()~`>#+\-=|{}.!])', r'\\\1', part)
            escaped_parts.append(escaped)
        else:
            # Markdown Entity: preserve boundaries but escape inner text for inline styles
            if part.startswith('```') or part.startswith('`') or part.startswith('['):
                escaped_parts.append(part)
            else:
                boundary_len = 2 if part.startswith('||') else 1
                boundary = part[:boundary_len]
                inner_text = part[boundary_len:-boundary_len]
                inner_escaped = re.sub(r'([\\_*\[\]()~`>#+\-=|{}.!])', r'\\\1', inner_text)
                escaped_parts.append(f"{boundary}{inner_escaped}{boundary}")
                
    return "".join(escaped_parts)


def normalize_url(url: str) -> str:
    """Return a canonical, lowercase URL with query parameters, fragments, and
    trailing slashes removed. Scheme, host, and full path are preserved intact.

    Validated safe for:
      - FT article slugs:        /content/abc-123-def-456
      - Bloomberg article slugs: /news/articles/2024-01-01/slug
      - Technoz article slugs:   /detail-news/slug
    """
    parsed = urlparse(url)
    clean_path = parsed.path.rstrip("/")
    return urlunparse(
        (parsed.scheme, parsed.netloc, clean_path, "", "", "")
    ).lower()

logger = logging.getLogger(__name__)


def sanitize_for_xml(text: str) -> str:
    """
    Neutralizes LLM-hallucinated typographic anomalies that crash ElementTree.

    Replaces:
    - Smart quotes and dashes with ASCII equivalents
    - Non-breaking spaces with regular spaces  
    - Unescaped ampersands with &

    Args:
        text: Raw text from LLM response

    Returns:
        Sanitized text safe for XML parsing
    """
    if not text:
        return ""

    replacements = {
        '\u2011': '-', '\u2013': '-', '\u2014': '-',
        '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"',
        '\u00A0': ' ', '&nbsp;': ' '
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    # Escape bare ampersands (negative lookahead ensures we don't break existing entities)
    text = re.sub(r'&(?![a-zA-Z0-9#]+;)', '&', text)
    return text.strip()


# Stateful Markdown/HTML-aware splitter to avoid breaking code fences and links
SAFE_MAX_LENGTH = 4000

# Keep legacy flat delimiter config for existing internal helpers
DELIMITER_CONFIG = {
    'tokens': ['```', '`', '*', '_', '['],
    'toggle': {'*', '_', '`', '```'},
    'compound': {'['}
}

# Multi-parse-mode-aware delimiter configuration used by the new splitter
DELIMITER_CONFIG_BY_MODE = {
    'Markdown': {
        'tokens':   ['```', '`', '*', '_', '['],
        'toggle':   {'*', '_', '`', '```'},
        'compound': {'['},
    },
    'MarkdownV2': {
        'tokens':   ['```', '||', '__', '`', '*', '_', '~', '[', '>'],
        'toggle':   {'*', '_', '__', '~', '||', '`', '```'},
        'compound': {'['},
        'structural': {'>'},
    },
}

DELIMITER_BUFFER = 50


def smart_split_message(
    text: str,
    max_length: int = SAFE_MAX_LENGTH,
    parse_mode: str = None
) -> List[str]:
    """
    Smartly split a message into chunks with
    parse-mode-aware state tracking.
    """
    if not text: return []
    if len(text) <= max_length: return [text]

    config = DELIMITER_CONFIG_BY_MODE.get(parse_mode)
    if not config:
        return _split_boundary_only(text, max_length)

    tokens    = sorted(config['tokens'], key=len, reverse=True)
    toggle_set = config.get('toggle', set())
    chunks: List[str] = []
    remaining = text
    stack: list = []
    carry_openers: str = ''
    carry_bq: bool = False

    while remaining:
        bq_cost    = 2 if (carry_bq and parse_mode == 'MarkdownV2') else 0
        carry_cost = len(carry_openers) + bq_cost
        if len(remaining) + carry_cost <= max_length:
            final_chunk = _assemble_chunk(
                remaining, carry_openers, closers='',
                in_bq=carry_bq, parse_mode=parse_mode
            )
            chunks.append(final_chunk)
            break

        effective_limit = max_length - carry_cost - DELIMITER_BUFFER
        split_idx = _find_split_point(remaining, effective_limit, max_length)

        chunk_stack = _scan_state(
            remaining, split_idx, list(stack), tokens, toggle_set
        )
        adjusted = _rollback_if_inside_atom(remaining, split_idx, tokens)
        if adjusted != split_idx:
            split_idx   = adjusted
            chunk_stack = _scan_state(
                remaining, split_idx, list(stack), tokens, toggle_set
            )

        closers      = _build_closers(chunk_stack)
        next_openers = _build_openers(chunk_stack)

        raw_content = remaining[:split_idx]
        chunk_text  = _assemble_chunk(
            raw_content, carry_openers, closers,
            in_bq=carry_bq, parse_mode=parse_mode
        )
        chunks.append(chunk_text)
        remaining     = remaining[split_idx:]
        stack         = chunk_stack
        carry_openers = next_openers
        carry_bq      = _in_blockquote_at(remaining, 0, parse_mode)

    return _merge_delimiter_only_chunks(chunks, max_length)


def _build_closers(stack: list) -> str:
    return ''.join(']' if t == '[' else t for t in reversed(stack))


def _build_openers(stack: list) -> str:
    return ''.join(t for t in stack if t != '[')


def _in_blockquote_at(text: str, idx: int, parse_mode: str) -> bool:
    if parse_mode != 'MarkdownV2': return False
    line_start = text.rfind('\n', 0, idx)
    line_start = 0 if line_start == -1 else line_start + 1
    return text[line_start:line_start+2] == '> '


def _merge_delimiter_only_chunks(chunks: List[str], max_length: int) -> List[str]:
    merged, i = [], 0
    while i < len(chunks):
        c = chunks[i]
        if c.strip() in {'', '```', '`', '*', '_'} and merged:
            candidate = merged[-1] + c
            if len(candidate) <= max_length:
                merged[-1] = candidate
                i += 1; continue
        merged.append(c)
        i += 1
    return merged


def _find_split_point(text: str, effective_limit: int, hard_max: int) -> int:
    ceiling  = min(effective_limit, len(text))
    floor_75 = int(ceiling * 0.75)
    for search_start in [floor_75, 0]:
        idx = text.rfind('\n', search_start, ceiling)
        if idx != -1: return idx + 1
        best = -1
        for punct in ['. ', '! ', '? ']:
            p = text.rfind(punct, search_start, ceiling)
            if p > best: best = p
        if best != -1: return best + 2
        idx = text.rfind(' ', search_start, ceiling)
        if idx != -1: return idx + 1
    return min(hard_max, len(text))


def _scan_state(text, end_idx, stack, tokens, toggle_set):
    i = 0
    while i < end_idx:
        if stack and stack[-1] in ('`', '```'):
            closer = stack[-1]
            if text.startswith(closer, i): stack.pop(); i += len(closer)
            else: i += 1
            continue
        if text[i] == '\\': i += 2; continue
        matched = next((t for t in tokens if text.startswith(t, i)), None)
        if not matched: i += 1; continue
        if matched in toggle_set:
            if stack and stack[-1] == matched: stack.pop()
            else: stack.append(matched)
        i += len(matched)
    return stack


def _assemble_chunk(raw_content, openers, closers, in_bq, parse_mode):
    nl_count = 0
    while nl_count < len(raw_content) and raw_content[nl_count] == '\n':
        nl_count += 1
    body = raw_content[nl_count:]
    bq = '> ' if (
        in_bq and parse_mode == 'MarkdownV2'
        and body and not body.startswith('>')
    ) else ''
    return raw_content[:nl_count] + bq + openers + body + closers


def _rollback_if_inside_atom(text: str, split_idx: int, tokens) -> int:
    for t in (tok for tok in tokens if len(tok) > 1):
        for offset in range(1, len(t)):
            start = split_idx - offset
            if start >= 0 and text.startswith(t, start):
                return start
    bracket_pos = text.rfind('[', 0, split_idx)
    if bracket_pos != -1:
        paren_close = text.find(')', bracket_pos)
        if paren_close != -1 and split_idx <= paren_close:
            return bracket_pos
    return split_idx


def _split_boundary_only(text: str, max_length: int) -> List[str]:
    # Plain-text fallback — no Markdown awareness needed
    if not text:
        return []
    if len(text) <= max_length: return [text]
    chunks, remaining = [], text
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining); break
        split = remaining.rfind('\n', 0, max_length)
        if split == -1: split = max_length
        chunks.append(remaining[:split])
        remaining = remaining[split:].lstrip()
    return chunks

# ── Public alias (keeps existing call-sites working) ────────────────────
def smart_split_telegram_message(
    text: str,
    max_length: int = SAFE_MAX_LENGTH,
    parse_mode: str = None
) -> List[str]:
    return smart_split_message(text, max_length, parse_mode)


# The rest of the original utilities are preserved (JSON parsing, HTML cleaning, summarization)

def parse_llm_json(text: str) -> Dict[str, Any]:
    """
    Extracts and parses a JSON object from a string that may be wrapped
    in Markdown code fences (```json ... ```).
    
    Includes sanitization to handle common LLM syntax errors like trailing commas.
    """
    try:
        json_match = re.search(
            r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE
        )
        json_string = json_match.group(1).strip() if json_match else text.strip()
        
        # Sanitize common JSON syntax errors from LLMs
        # 1. Remove trailing commas before closing braces/brackets
        json_string = re.sub(r',\s*([}\]])', r'\1', json_string)
        # 2. Handle unescaped control characters (very basic protection)
        # Note: This is a simplified fix. Full JSON5 parsing would be better but adds dependencies.
        
        if not json_string:
            return {}
            
        return json.loads(json_string)
    except json.JSONDecodeError:
        log.dual_log(
            tag="Text:Parse",
            message="Failed to parse JSON from AI response.",
            level="ERROR",
            payload={"event_type": "json.parse_error", "raw_content": text[:500]},
            exc_info=True,
        )
        return {}


def extract_xml_tag(text: str, tag: str) -> str:
    pattern = rf"<{tag}>(.*?)</{tag}>"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""


async def map_reduce_summarize(
    text: str, 
    llm_client, 
    chunk_size: int = 25000, 
    overlap: int = 2500
) -> str:
    """
    Asynchronously chunks massive text, summarizes parts, and synthesizes a final document.

    This implements the map-reduce pattern for handling text that exceeds LLM context windows.

    Args:
        text: Raw text to process
        llm_client: LLM client instance with complete_chat method
        chunk_size: Size of each text chunk
        overlap: Number of overlapping characters between chunks

    Returns:
        Synthesized summary of the entire text
    """
    if not text:
        return ""
    if len(text) <= chunk_size:
        return text
    start = 0
    chunks = []
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += (chunk_size - overlap)
        if start >= len(text) or chunk_size <= overlap:
            break

    async def summarize_chunk(chunk_text: str, index: int) -> str:
        from utils.text_processing import escape_prompt_separators
        from tools.scraper.summary_prompts import MAP_REDUCE_SUMMARIZE_CHUNK_PROMPT
        prompt = MAP_REDUCE_SUMMARIZE_CHUNK_PROMPT.format(index=index, chunk_text=escape_prompt_separators(chunk_text))
        from clients.llm import LLMRequest
        res = await llm_client.complete_chat(
            LLMRequest(messages=[{"role": "user", "content": prompt}])
        )
        return res.content

    chunk_summaries = await asyncio.gather(
        *[summarize_chunk(chunk, i) for i, chunk in enumerate(chunks)]
    )

    combined_summaries = "\n\n".join(chunk_summaries)
    from utils.text_processing import escape_prompt_separators
    from tools.scraper.summary_prompts import MAP_REDUCE_SYNTHESIZE_PROMPT
    reduce_prompt = MAP_REDUCE_SYNTHESIZE_PROMPT.format(combined_summaries=escape_prompt_separators(combined_summaries))

    from clients.llm import LLMRequest
    final_res = await llm_client.complete_chat(
        LLMRequest(messages=[{"role": "user", "content": reduce_prompt}])
    )

    return final_res.content


def clean_html_for_agent(html_content: str, max_chars: int = 40000,
                          extra_attrs: set | None = None) -> str:
    from bs4 import BeautifulSoup, Comment

    if not html_content:
        return ""

    soup = BeautifulSoup(html_content, "html.parser")
    noise_tags = [
        "style", "script", "link", "meta", "noscript",
        "svg", "iframe", "footer",
    ]
    for tag in soup(noise_tags):
        tag.decompose()

    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # strip SoM badge overlays before slimming
    for _badge in soup.find_all("div", attrs={"data-ai-badge": True}):
        _badge.decompose()

    _base_attrs = {
        "href", "id", "class", "name", "type",
        "aria-label", "role", "placeholder",
    }
    allowed_attrs = _base_attrs | (extra_attrs or set())
    
    for tag in soup.find_all(True):
        attrs = dict(tag.attrs)
        for attr in attrs:
            if attr not in allowed_attrs:
                del tag[attr]

    text = str(soup)
    slimmed = " ".join(text.split())

    if len(slimmed) > max_chars:
        log.dual_log(
            tag="Text:CleanHTML",
            message=f"HTML truncated to {max_chars} chars.",
            level="INFO",
            payload={"event_type": "html.truncate", "original_size": len(slimmed)},
        )
        return slimmed[:max_chars] + "... [TRUNCATED]"
    return slimmed


def validate_args(args: dict, required_keys: list[str]) -> Optional[str]:
    """Validate that the provided args dict contains the required keys.

    Returns None when valid, otherwise a human-readable error string.
    """
    missing = []
    for key in required_keys:
        if key not in args:
            missing.append(key)
            continue
        val = args.get(key)
        if val is None:
            missing.append(key)
            continue
        if isinstance(val, str) and not val.strip():
            missing.append(key)
    if missing:
        return f"Missing or invalid required args: {', '.join(missing)}"
    return None
