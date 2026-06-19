# utils/logger/formatters.py
import json
import logging
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
        # short strings pass through
        return payload
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
