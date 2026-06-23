# utils/observability/masking.py
"""Recursive truncation and masking for activity inputs/outputs.

Implements the convention's truncation rules:
1. Per-key truncation at 50,000 chars (configurable).
2. Auto-masking of bulky/sensitive patterns BEFORE truncation.
3. Structural recursion through dicts and lists.
4. Cycle detection via id() tracking.
5. Depth limit at 10 levels.

Ref: convention §4.3.d
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Default per-key character cap.
# Per convention: "Default cap: 50,000 characters per key value."
DEFAULT_MAX_CHARS = 50_000

# Maximum nesting depth before truncation.
MAX_DEPTH = 10

# --- Pattern detection for bulky/sensitive strings ---

# Base64: long runs of base64 alphabet (1000+ chars).
# Per convention: "Base64-encoded blobs (long runs of base64 alphabet
# characters, typically above a threshold such as 1,000 chars)."
_BASE64_PATTERN = re.compile(r"^[A-Za-z0-9+/=\s]{1000,}$")

# Embeddings: long arrays of floats (10+ comma-separated floats).
# Per convention: "Vector embeddings (long arrays/lists of floating-point
# numbers, or strings that look like serialized float arrays)."
_EMBEDDING_PATTERN = re.compile(
    r"^\[?-?\d+\.?\d*(?:[eE][+-]?\d+)? "
    r"(?:[\s,]+-?\d+\.?\d*(?:[eE][+-]?\d+)?){9,}\]?$"
)

# Secrets: API keys, JWTs, etc.
# Per convention: "PII / secret patterns (API keys, JWTs, passwords, credit-card
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
# Per convention: auto-masking runs on values; key-level masking is an
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
    # Per convention: "Binary blobs (strings containing non-printable characters)."
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

    Rules (per convention §4.3.d):
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


def serialize_safe(obj: Any, max_chars: int = DEFAULT_MAX_CHARS) -> Any:
    """Top-level orchestrator: walk obj with truncate_and_mask, then verify
    JSON-serializability. Non-serializable objects fall back to repr().

    This function NEVER raises.
    """
    try:
        masked = truncate_and_mask(obj, max_chars=max_chars)
        # Verify JSON-serializable. If not, fall through to repr fallback.
        import json
        json.dumps(masked, default=str)
        return masked
    except Exception:
        return f"<non-serializable: {type(obj).__name__}>"
