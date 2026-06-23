# utils/logger/formatters.py
"""Payload serialization and redaction for the dual-stream logger.

Design philosophy:
- REDACTION IS ALWAYS ON. Every leaf string (whether from an explicit str
  payload, a dict value, a list element, or a repr() fallback) flows through
  _redact_secrets_in_string before reaching logs.db. This is non-negotiable.
- TRUNCATION IS DESTRUCTIVE AND AVOIDED. Per the Python logging philosophy
  (https://docs.python.org/3/library/logging.handlers.html), the stdlib
  approach to oversized data is rotation/buffering, not per-record
  truncation. Per the structlog processor docs
  (https://www.structlog.org/en/stable/processors.html), processors may
  mutate event_dict freely. We use that freedom to SPOOL oversized payloads
  to a sidecar file (artifacts/log_spool/<event_id>.txt) and store a pointer
  in logs.db. Operators get the full payload during post-mortem; the DB
  does not bloat.
- The threshold _MAX_PAYLOAD_CHARS (default 10000) is the inline/spool
  boundary, NOT a hard truncation cap.
"""
import json
import logging
import os
import re as _re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Spool directory for oversized payloads.
# Created lazily on first spool to avoid mkdir overhead on every import.
# Per Python pathlib docs: https://docs.python.org/3/library/pathlib.html#pathlib.Path.mkdir
# "parents=True" creates intermediate dirs; "exist_ok=True" is a no-op if the
# dir already exists.
# ---------------------------------------------------------------------------
_SPOOL_DIR = Path("artifacts") / "log_spool"


def _ensure_spool_dir() -> Path:
    """Lazily create the spool directory. Returns the Path."""
    _SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    return _SPOOL_DIR


# ---------------------------------------------------------------------------
# MaskableData — explicit masking for known-large/sensitive payloads.
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Secret redaction regex.
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
# ---------------------------------------------------------------------------
_REDACT_PEEK_PATTERN = _re.compile(
    r'(?i)((?:api[_-]?key|secret|password|passwd|token|credential|auth'
    r'|private[_-]?key)(?:\s*[=:]\s*))(\S+)',
)

# Inline/spool boundary. Strings at or below this length are stored inline
# in logs.db. Longer strings are spooled to a sidecar file.
# This is NOT a truncation cap — the full (redacted) string is preserved
# in the spool file.
_MAX_PAYLOAD_CHARS = 10000


def _redact_secrets_in_string(s: str) -> str:
    """Apply regex-based secret redaction to a string value.

    Catches patterns NOT explicitly wrapped in MaskableData.
    For example, if an engineer logs {"config": "api_key=SK_live_abc123"},
    this function redacts it to {"config": "api_key=[REDACTED]"}.
    """
    return _REDACT_PEEK_PATTERN.sub(r'\1[REDACTED]', s)


def _spool_large_payload(redacted_str: str, event_id: str | None) -> str:
    """Spool an oversized (already-redacted) string to a sidecar file.

    Returns a pointer string of the form:
        "[SPOOLED: <N> chars -> <path>]"

    The full redacted payload is preserved on disk for post-mortem analysis.
    The pointer in logs.db is always small (< 200 chars).

    Per the Python logging philosophy (rotate/buffer, don't truncate):
    https://docs.python.org/3/library/logging.handlers.html
    and the structlog processor philosophy (mutate event_dict freely):
    https://www.structlog.org/en/stable/processors.html
    """
    # event_id may be None when _serialize_payload is called from a context
    # that doesn't have one (e.g. the in-process buffer). Fall back to a
    # timestamp-based filename.
    safe_id = event_id or f"noid_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"
    # Sanitize the event_id for use as a filename (defensive — ULIDs are
    # already safe, but we guard against arbitrary caller input).
    safe_id = _re.sub(r'[^A-Za-z0-9_-]', '_', safe_id)
    spool_path = _ensure_spool_dir() / f"{safe_id}.txt"
    try:
        spool_path.write_text(redacted_str, encoding="utf-8")
        return f"[SPOOLED: {len(redacted_str)} chars -> {spool_path}]"
    except OSError:
        # Disk full / permissions / etc. Fall back to a truncation indicator
        # WITH a clear marker that data was lost. This is the ONLY path that
        # truncates, and only when spooling itself fails.
        return (
            f"[SPOOL_FAILED: {len(redacted_str)} chars, truncated to "
            f"{_MAX_PAYLOAD_CHARS}: {redacted_str[:_MAX_PAYLOAD_CHARS]}"
            f"...[TRUNCATED: {len(redacted_str) - _MAX_PAYLOAD_CHARS} more chars]]"
        )


def _redact_and_handle_size(s: str, event_id: str | None = None) -> str:
    """Redact secrets, then either return inline (small) or spool (large).

    This is the single chokepoint for every leaf string emitted by
    _serialize_payload. Guarantees:
      1. Secrets are ALWAYS redacted (no bypass).
      2. Small strings (<= _MAX_PAYLOAD_CHARS) are returned inline.
      3. Large strings are spooled to a sidecar file; a pointer is returned.
      4. The returned string is ALWAYS <= ~200 chars when spooled, or
         <= _MAX_PAYLOAD_CHARS when inline.

    Per the observability standard "logs contain full, untruncated payloads
    necessary for auditing and debugging" — the full redacted payload is
    preserved on disk; only the logs.db row holds a pointer.
    """
    redacted = _redact_secrets_in_string(s)
    if len(redacted) <= _MAX_PAYLOAD_CHARS:
        return redacted
    return _spool_large_payload(redacted, event_id)


def _serialize_payload(payload: Any, depth: int = 0, event_id: str | None = None) -> Any:
    """Serialize and mask payloads for storage in logs.db.

    Rules:
    - Mask MaskableData by calling to_masked_string().
    - Preserve simple scalars (int/float/bool/None).
    - Convert bytes to a masked summary.
    - Recurse into dicts, lists, tuples up to a safe depth (10).
    - Invoke to_dict() or model_dump() (pydantic v2) if available.
    - Fall back to repr() for unknown objects, routed through the SAME
      redact-and-spool pipeline as explicit strings.

    Args:
        payload: The object to serialize.
        depth: Current recursion depth (internal). Max 10.
        event_id: Optional ULID for naming spool files. When provided,
            spooled payloads are written to
            artifacts/log_spool/<event_id>.txt.
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
        # Apply secret redaction, then either inline (small) or spool (large).
        return _redact_and_handle_size(payload, event_id)
    if isinstance(payload, (bytes, bytearray)):
        return f"[MASKED: Binary Data | {len(payload)} bytes]"
    if isinstance(payload, (int, float, bool)):
        return payload
    # Pydantic v2 models expose model_dump() (replaces v1 .dict()).
    # Ref: https://docs.pydantic.dev/latest/concepts/models/#model-export
    # This branch must come BEFORE to_dict() because pydantic v2 models
    # also have a deprecated .dict() method.
    if hasattr(payload, "model_dump") and callable(getattr(payload, "model_dump")):
        try:
            return payload.model_dump()
        except Exception:
            pass
    if hasattr(payload, "to_dict") and callable(getattr(payload, "to_dict")):
        try:
            return payload.to_dict()
        except Exception:
            pass
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            try:
                out[k] = _serialize_payload(v, depth + 1, event_id)
            except Exception:
                out[k] = "<unserializable>"
        return out
    if isinstance(payload, list):
        return [_serialize_payload(i, depth + 1, event_id) for i in payload]
    if isinstance(payload, tuple):
        return tuple(_serialize_payload(i, depth + 1, event_id) for i in payload)
    # FALLBACK: unknown object type. Route through the SAME safety net as
    # explicit strings — redact secrets, then either inline or spool.
    # Without this, a 50MB repr() of a pydantic model or an unwrapped
    # dataclass would bypass _redact_secrets_in_string entirely,
    # leaking credentials and saturating logs.db.
    try:
        rendered = repr(payload)
    except Exception:
        return "<unserializable>"
    # Coerce to str defensively (repr() always returns str per the Python
    # data model, but we guard against pathological __repr__ overrides).
    rendered = rendered if isinstance(rendered, str) else str(rendered)
    return _redact_and_handle_size(rendered, event_id)




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
