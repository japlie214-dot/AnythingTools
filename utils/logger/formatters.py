# utils/logger/formatters.py
import json
import logging
import re as _re
from datetime import datetime, timezone
from typing import Any


class MaskableData:
    """Base class for payloads that should be masked in logs.

    Subclass and implement to_masked_string() to provide a compact
    placeholder for large or sensitive payloads (e.g. base64 images).
    """

    def to_masked_string(self) -> str:
        return "[MASKED: MaskableData]"


class Base64Image(str, MaskableData):
    """Wrap a base64 image string so the logger masks it instead of
    emitting the full blob.
    """

    def to_masked_string(self) -> str:
        return f"[MASKED: Base64Image | {len(self)} chars]"


def _serialize_payload(payload: Any, depth: int = 0) -> Any:
    """Serialize and mask payloads for storage in logs.db.

    Rules:
    - Mask MaskableData by calling to_masked_string().
    - Preserve simple scalars (int/float/bool/None).
    - Convert bytes to a masked summary.
    - Recurse into dicts, lists, tuples up to a safe depth.
    - Fall back to repr() or "<unserializable>" when needed.
    """
    if depth > 10:
        return "[MAX_DEPTH_EXCEEDED]"
    if payload is None:
        return None
    if isinstance(payload, MaskableData):
        try:
            return payload.to_masked_string()
        except Exception:
            return "[MASKED]"
    if isinstance(payload, str):
        # Apply secret redaction as a safety net for values not
        # explicitly wrapped in MaskableData, then truncate if huge.
        result = _redact_secrets_in_string(payload)
        if len(result) > _MAX_PAYLOAD_CHARS:
            return result[:_MAX_PAYLOAD_CHARS] + f"...[TRUNCATED: {len(result) - _MAX_PAYLOAD_CHARS} more chars]"
        return result
    if isinstance(payload, (bytes, bytearray)):
        return f"[MASKED: Binary Data | {len(payload)} bytes]"
    if isinstance(payload, (int, float, bool)):
        return payload
    if hasattr(payload, "to_dict") and callable(getattr(payload, "to_dict")):
        try:
            return payload.to_dict()
        except Exception:
            pass
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            try:
                out[k] = _serialize_payload(v, depth + 1)
            except Exception:
                out[k] = "<unserializable>"
        return out
    if isinstance(payload, list):
        return [_serialize_payload(i, depth + 1) for i in payload]
    if isinstance(payload, tuple):
        return tuple(_serialize_payload(i, depth + 1) for i in payload)
    try:
        return repr(payload)
    except Exception:
        return "<unserializable>"
    
    
    # Regex pattern for automatic redaction of common secret patterns.
    # This is a SAFETY NET: it catches credentials that engineers forgot
    # to wrap in MaskableData. It runs on every serialized payload value
    # that is a string, checking for patterns like:
    #   - api_key=abc123  / api_key: abc123
    #   - password=secret  / password: secret
    #   - token=xyz  / token: xyz
    #   - Bearer abc123def
    #
    # The pattern requires a structural delimiter (= or :) after the key
    # name, which prevents false positives on words like "keyboard".
_REDACT_PEEK_PATTERN = _re.compile(
    r'(?i)((?:api[_-]?key|secret|password|passwd|token|credential|auth'
    r'|private[_-]?key)(?:\s*[=:]\s*))(\S+)',
)

# Maximum payload string length before truncation.
# Prevents unbounded memory from huge binary blobs or embedding arrays.
_MAX_PAYLOAD_CHARS = 10000


def _redact_secrets_in_string(s: str) -> str:
    """Apply regex-based secret redaction to a string value.
    
    Catches patterns NOT explicitly wrapped in MaskableData.
    For example, if an engineer logs {"config": "api_key=SK_live_abc123"},
    this function redacts it to {"config": "api_key=[REDACTED]"}.
    """
    return _REDACT_PEEK_PATTERN.sub(r'\1[REDACTED]', s)


def _mask_payload_if_large(payload: Any) -> Any:
    """Safety valve to prevent massive binary blobs from saturating storage.
    
    If the serialized form of a payload exceeds _MAX_PAYLOAD_CHARS,
    it is truncated with a clear indicator. This prevents a single
    erroneous log entry (e.g., an entire embedding array) from
    consuming excessive storage in logs.db.
    """
    if payload is None:
        return None
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > _MAX_PAYLOAD_CHARS:
            return f"[MASKED: Binary Data | {len(payload)} bytes]"
    if isinstance(payload, str):
        if len(payload) > _MAX_PAYLOAD_CHARS:
            return payload[:_MAX_PAYLOAD_CHARS] + f"...[TRUNCATED: {len(payload) - _MAX_PAYLOAD_CHARS} more chars]"
    return payload


class ConsoleFormatter(logging.Formatter):
    """Console formatter: Timestamp | Level (color) | Tag | Message.

    Attached to the StreamHandler in utils/logger/handlers.py.
    """

    COLORS = {
        "DEBUG": "\u001b[36m",
        "INFO": "\u001b[32m",
        "WARNING": "\u001b[33m",
        "ERROR": "\u001b[31m",
        "CRITICAL": "\u001b[35m",
    }
    RESET = "\u001b[0m"

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        color = self.COLORS.get(record.levelname, "")
        reset = self.RESET if color else ""
        tag = getattr(record, "tag", "GENERIC")
        s = f"{ts} | {color}{record.levelname:<8}{reset} | {tag:<15} | {record.getMessage()}"
        if record.exc_info:
            if not getattr(record, "exc_text", None):
                try:
                    record.exc_text = self.formatException(record.exc_info)
                except Exception:
                    record.exc_text = None
        if getattr(record, "exc_text", None):
            if not s.endswith("\n"):
                s = s + "\n"
            s = s + record.exc_text
        return s
