# utils/logger/formatters.py
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from utils.id_generator import ULID
from utils.logger.state import _log_config  # DAG-compliant: state is upstream

_REDACTED_PEEK_KEYS: frozenset[str] = frozenset({
    "authorization", "api-key", "api_key", "x-api-key",
    "token", "secret", "password", "bearer",
})

_REDACT_PEEK_PATTERN: re.Pattern = re.compile(
    r'("(?i:'
    + "|".join(sorted(_REDACTED_PEEK_KEYS))
    + r')"\s*:\s*)(?:(?:\"[^\"]*(?:\"|$))|(?:[^\s,}\]]+))'
)

_REDACT_B64_PATTERN: re.Pattern = re.compile(
    r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+'
)


def _mask_payload_if_large(serialized_str: str, original_payload: Any) -> str:
    """Return a [MASKED] placeholder if serialized_str exceeds LOGGER_TRUNCATION_LIMIT.
    Called exclusively from FileFormatter.format(); never from _serialize_payload
    or the _tool_log_buffer append path.
    """
    limit = getattr(_log_config, 'LOGGER_TRUNCATION_LIMIT', 5_000_000) if _log_config else 5_000_000
    n = len(serialized_str)
    if n <= limit:
        return serialized_str
    if isinstance(original_payload, dict):
        keys = list(original_payload.keys())
        keys_preview = ", ".join(str(k) for k in keys[:5])
        if len(keys) > 5:
            keys_preview += ", ..."
        desc = f"dict(keys=[{keys_preview}])"
    elif isinstance(original_payload, list):
        desc = f"list[{len(original_payload)}]"
    else:
        desc = type(original_payload).__name__
    peek_len = getattr(_log_config, 'LOGGER_PEEK_LENGTH', 100) if _log_config else 100
    raw_peek = serialized_str[:peek_len]
    redacted_peek = _REDACT_PEEK_PATTERN.sub(r'\1"[REDACTED]"', raw_peek)
    return f"[MASKED: {n} chars | type: {desc} | Peek: {redacted_peek}...]"


class MaskableData:
    """
    Semantic Type Wrapper Base Interface.
    
    OPERATIONAL DOCUMENTATION:
    For Future Developers: Instead of relying on expensive and fragile regular expressions
    to sanitize logs, wrap sensitive objects (like base64 strings or high-dimensional embeddings)
    in a subclass of `MaskableData` at their origin.
    
    How to use:
    1. If you introduce a new binary format or large payload, subclass `MaskableData`.
    2. Override the `to_masked_string()` method to return a safe placeholder.
    3. Wrap your data (e.g., `payload={"image": Base64Image(b64_string)}`).
    The logger will automatically intercept and mask it in O(1) time without regex.
    """
    def to_masked_string(self) -> str:
        return "[MASKED: MaskableData]"

class SensitiveVector(list, MaskableData):
    def to_masked_string(self) -> str:
        return f"[MASKED: SensitiveVector | {len(self)} dims]"

class SensitiveBytes(bytes, MaskableData):
    def to_masked_string(self) -> str:
        return f"[MASKED: SensitiveBytes | {len(self)} bytes]"

class Base64Image(str, MaskableData):
    def to_masked_string(self) -> str:
        return f"[MASKED: Base64Image | {len(self)} chars]"

def _serialize_payload(payload: Any, depth: int = 0) -> Any:
    """Three-tier fallback: JSON-native → repr() → '<unserializable>'."""
    if depth > 10:
        return "[MAX_DEPTH_EXCEEDED]"
    if payload is None:
        return None
    if isinstance(payload, MaskableData):
        return payload.to_masked_string()
    if isinstance(payload, str):
        if len(payload) > 1000 and payload.startswith("data:image/"):
            return f"[MASKED: Legacy Base64Image | {len(payload)} chars]"
        return payload
    if isinstance(payload, (bytes, bytearray)):
        return f"[MASKED: Binary Data | {len(payload)} bytes]"
    if isinstance(payload, (int, float, bool)):
        return payload
    if hasattr(payload, "to_dict"):
        try:
            return payload.to_dict(orient="records")
        except Exception:
            pass
    if isinstance(payload, dict):
        return {k: _serialize_payload(v, depth + 1) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_serialize_payload(i, depth + 1) for i in payload]
    if isinstance(payload, tuple):
        return tuple(_serialize_payload(i, depth + 1) for i in payload)
    try:
        return repr(payload)
    except Exception:
        pass
    return "<unserializable>"


class PayloadOrErrorFilter(logging.Filter):
    """Allow only log records that carry a payload or exception info.
    Attached exclusively to file handlers so that status-only messages are
    silently dropped from the file stream while remaining visible on the console.
    """
    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        if getattr(record, "payload", None) is not None:
            return True
        exc = getattr(record, "exc_info", None)
        if exc is not None and isinstance(exc, tuple) and exc[0] is not None:
            return True
        return False


class FileFormatter(logging.Formatter):
    """Single-line JSON formatter for master and specialized file output."""
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        log_entry: dict[str, Any] = {
            "event_id": getattr(record, "event_id", ULID.generate()),
            "timestamp": timestamp,
            "level": record.levelname,
            "tag": getattr(record, "tag", "Unknown"),
            "message": record.getMessage(),
        }
        payload = getattr(record, "payload", None)
        if payload is not None:
            serialized_obj = _serialize_payload(payload)
            serialized_str = json.dumps(serialized_obj, ensure_ascii=False, default=str)
            masked = _mask_payload_if_large(serialized_str, payload)
            log_entry["payload"] = serialized_obj if masked is serialized_str else masked
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, exc_value, _ = record.exc_info
            log_entry["error"] = {
                "type": exc_type.__name__,
                "message": str(exc_value),
                "traceback": self.formatException(record.exc_info),
            }
        final_json = json.dumps(log_entry, ensure_ascii=False, default=str)
        return _REDACT_B64_PATTERN.sub(
            'data:image/[MASKED];base64,[STATIC_PLACEHOLDER]', final_json
        )


class ConsoleFormatter(logging.Formatter):
    """Quartet: Timestamp | Level (color) | Tag | Message. Payload excluded."""
    COLORS = {
        "DEBUG":    "\u001b[36m",
        "INFO":     "\u001b[32m",
        "WARNING":  "\u001b[33m",
        "ERROR":    "\u001b[31m",
        "CRITICAL": "\u001b[35m",
    }
    RESET = "\u001b[0m"

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        color = self.COLORS.get(record.levelname, "")
        reset = self.RESET if color else ""
        tag = getattr(record, "tag", "Unknown")
        s = f"{ts} | {color}{record.levelname:<8}{reset} | {tag} | {record.getMessage()}"
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
