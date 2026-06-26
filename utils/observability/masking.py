# utils/observability/masking.py
"""Recursive truncation and masking for activity inputs/outputs.

Implements the Developer Contract's truncation rules (see utils/observability/__init__.py §4.3.d):
1. Per-key truncation at 50,000 chars (configurable).
2. Auto-masking of bulky/sensitive patterns BEFORE truncation.
3. Structural recursion through dicts and lists.
4. Cycle detection via id() tracking.
5. Depth limit at 10 levels.

Ref: Developer Contract in utils/observability/__init__.py §4.3.d
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Default per-key character cap.
# Per Developer Contract §4.3.d: "Default cap: 50,000 characters per key value."
DEFAULT_MAX_CHARS = 50_000

# Maximum nesting depth before truncation.
MAX_DEPTH = 10

# --- Pattern detection for bulky/sensitive strings ---

# Base64: long runs of base64 alphabet (1000+ chars).
# Per Developer Contract §4.3.d: "Base64-encoded blobs (long runs of base64 alphabet
# characters, typically above a threshold such as 1,000 chars)."
_BASE64_PATTERN = re.compile(r"^[A-Za-z0-9+/=\s]{1000,}$")

# Embeddings: long arrays of floats (10+ comma-separated floats).
# Per Developer Contract §4.3.d: "Vector embeddings (long arrays/lists of floating-point
# numbers, or strings that look like serialized float arrays)."
_EMBEDDING_PATTERN = re.compile(
    r"^\[?-?\d+\.?\d*(?:[eE][+-]?\d+)? "
    r"(?:[\s,]+-?\d+\.?\d*(?:[eE][+-]?\d+)?){9,}\]?$"
)

# Secrets: API keys, JWTs, etc.
# Per Developer Contract §4.3.d: "PII / secret patterns (API keys, JWTs, passwords, credit-card
# numbers) — match against the project's existing secret-detection rules."
_SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),           # OpenAI API key
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),           # GitHub personal access token
    re.compile(r"gho_[a-zA-Z0-9]{36}"),           # GitHub OAuth token
    re.compile(r"AKIA[A-Z0-9]{16}"),              # AWS access key ID
    re.compile(r"eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+"),  # JWT
    re.compile(r"\b\d{13,16}\b"),                 # Credit card number (13-16 digits)
]

# Sensitive key names — masked at the key level regardless of value.
# Per Developer Contract §4.3.d: auto-masking runs on values; key-level masking is an
# additional defense for known-sensitive field names.
_SECRET_KEY_NAMES = frozenset({
    "api_key", "apikey", "api-key",
    "token", "access_token", "refresh_token", "session_token",
    "password", "passwd", "pwd",
    "secret", "client_secret",
    "authorization", "auth",
    "credential", "credentials",
    "private_key", "privatekey",
    "access_key", "secret_key",
    "snowflake_private_key", "snowflake_password",
    "azure_openai_key", "azure_key",
    "chutes_api_token", "chutes_key",
    "telegram_bot_token",
})


def _is_bulky_or_sensitive(value: str) -> bool:
    """Detect strings that should be auto-masked per the convention."""
    if _BASE64_PATTERN.match(value):
        return True
    if _EMBEDDING_PATTERN.match(value):
        return True
    for pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            return True
    # Binary blob: non-printable characters in the first 100 chars.
    # Per Developer Contract §4.3.d: "Binary blobs (strings containing non-printable characters)."
    for c in value[:100]:
        if ord(c) < 32 and c not in "\n\r\t":
            return True
    return False


def _infer_data_type(value: str) -> str:
    """Infer the type label for the [MASKED: ...] placeholder."""
    if _BASE64_PATTERN.match(value):
        return "base64"
    if _EMBEDDING_PATTERN.match(value):
        return "embeddings"
    for pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            return "secret"
    for c in value[:100]:
        if ord(c) < 32 and c not in "\n\r\t":
            return "binary-blob"
    return "unknown"


def truncate_and_mask(
    value: Any,
    max_chars: int = DEFAULT_MAX_CHARS,
    _depth: int = 0,
    _seen: Optional[set] = None,
) -> Any:
    """Recursively walk value, masking sensitive keys and truncating long strings.

    Rules (per Developer Contract in utils/observability/__init__.py §4.3.d):
    1. Dict keys matching _SECRET_KEY_NAMES → value replaced with "[MASKED: secret-key]".
    2. String values matching bulky/sensitive patterns → replaced with
       "[MASKED: <type> - <len> chars]".
    3. String values exceeding max_chars → truncated to max_chars + "... [TRUNCATED N CHARS]".
    4. Dicts/lists traversed recursively; cycles detected via id() tracking.
    5. Depth beyond MAX_DEPTH → "<truncated: max-depth-exceeded>".

    This function NEVER raises — on any error, it returns a string repr.
    """
    if _seen is None:
        _seen = set()

    # Depth guard.
    if _depth > MAX_DEPTH:
        return "<truncated: max-depth-exceeded>"

    try:
        if isinstance(value, dict):
            obj_id = id(value)
            if obj_id in _seen:
                return "<cycle-detected>"
            _seen.add(obj_id)
            try:
                result = {}
                for k, v in value.items():
                    if isinstance(k, str) and k.lower() in _SECRET_KEY_NAMES:
                        result[k] = "[MASKED: secret-key]"
                    else:
                        result[k] = truncate_and_mask(v, max_chars, _depth + 1, _seen)
                return result
            finally:
                _seen.discard(obj_id)

        if isinstance(value, list):
            obj_id = id(value)
            if obj_id in _seen:
                return "<cycle-detected>"
            _seen.add(obj_id)
            try:
                return [truncate_and_mask(item, max_chars, _depth + 1, _seen) for item in value]
            finally:
                _seen.discard(obj_id)

        if isinstance(value, str):
            if _is_bulky_or_sensitive(value):
                return f"[MASKED: {_infer_data_type(value)} - {len(value)} chars]"
            if len(value) > max_chars:
                return value[:max_chars] + f"... [TRUNCATED {len(value) - max_chars} CHARS]"
            return value

        # Non-string scalars (int, float, bool, None) pass through unchanged.
        return value

    except Exception:
        # NEVER raise — the accumulator must not break tool execution.
        return f"<masking-error: {type(value).__name__}>"


def _cap_top_level_value(value: Any, max_chars: int = DEFAULT_MAX_CHARS) -> Any:
    """Cap the serialized size of the top-level value after structural recursion.
    
    Per Developer Contract §4.3.d Rule 6: per-key truncation at 50,000 chars.
    The threshold applies per individual top-level key value (for dicts) or
    per top-level value (for lists and other types) — NOT to the total payload.
    A 500,000-char payload where each individual top-level key's value is
    under 50,000 chars is recorded in full.
    
    Behavior:
    - dict: for each (k, v), compute len(json.dumps(v, default=str)); if > max_chars,
      replace v with f"[MASKED: per-key-cap-exceeded - {n} chars]". The key name
      is preserved (the placeholder is the VALUE, not the key).
    - list: compute len(json.dumps(value, default=str)); if > max_chars, replace
      the ENTIRE list with f"[MASKED: list-cap-exceeded - {n} chars, {len(value)} items]".
      A list has no keys, so per-key truncation does not apply; the list itself
      is treated as a single value to cap. Capping individual elements would not
      bound the total size (10,000 small elements = 10MB total), defeating the purpose.
    - other types (str, int, float, bool, None): pass through unchanged. Strings
      are already capped by truncate_and_mask; scalars are small.
    
    This function NEVER raises — on any error, it returns the input unchanged
    (the accumulator must not break tool execution).
    
    Ref: https://docs.python.org/3/library/json.html#json.dumps
    """
    import json
    try:
        if isinstance(value, dict):
            result = {}
            for k, v in value.items():
                # Strings are already capped to max_chars + a short truncation suffix
                # by truncate_and_mask (line 159: value[:max_chars] + "... [TRUNCATED N CHARS]").
                # Running them through json.dumps here adds 2 chars (the JSON quotes) plus
                # the suffix length, which pushes just-truncated strings (e.g. a 50,030-char
                # truncated string → 50,032 serialized) over max_chars and causes incorrect
                # full masking via the per-key-cap-exceeded placeholder.
                # Bypass the cap check for strings, but still assign result[k] = v —
                # a bare `continue` would silently drop the key from the Lineage payload,
                # destroying lineage data. Per Pushback 3 in the plan review.
                # Ref: https://docs.python.org/3/library/json.html#json.dumps
                if isinstance(v, str):
                    result[k] = v
                    continue
                try:
                    serialized = json.dumps(v, default=str, ensure_ascii=False)
                    if len(serialized) > max_chars:
                        result[k] = f"[MASKED: per-key-cap-exceeded - {len(serialized)} chars]"
                    else:
                        result[k] = v
                except (TypeError, ValueError):
                    # If serialization fails for one key, keep the original value
                    # (truncate_and_mask already processed it). The accumulator's
                    # outer try/except will catch any remaining issues.
                    result[k] = v
            return result
        if isinstance(value, list):
            try:
                serialized = json.dumps(value, default=str, ensure_ascii=False)
                if len(serialized) > max_chars:
                    return f"[MASKED: list-cap-exceeded - {len(serialized)} chars, {len(value)} items]"
            except (TypeError, ValueError):
                pass
            return value
        return value
    except Exception:
        return value


def serialize_safe(obj: Any, max_chars: int = DEFAULT_MAX_CHARS) -> Any:
    """Top-level orchestrator: walk obj with truncate_and_mask, then verify
    JSON-serializability. Non-serializable objects fall back to repr().

    This function NEVER raises.
    """
    try:
        masked = truncate_and_mask(obj, max_chars=max_chars)
        # Apply per-key truncation at the top level. truncate_and_mask caps
        # individual string values within the structure; _cap_top_level_value
        # caps each top-level key's serialized value (for dicts) or the entire
        # top-level value (for lists). Together they bound the lineage payload
        # size without affecting small payloads.
        masked = _cap_top_level_value(masked, max_chars=max_chars)
        # Verify JSON-serializable. If not, fall through to repr fallback.
        import json
        json.dumps(masked, default=str)
        return masked
    except Exception:
        return f"<non-serializable: {type(obj).__name__}>"
