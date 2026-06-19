# tests/test_logger_redaction.py
"""Verify _serialize_payload redacts secrets and SPOOLS (not truncates) oversized
payloads, including the repr() fallback path for unknown objects.

Regression test for Violation B + Pushback 5: the repr() fallback previously
bypassed both _redact_secrets_in_string and _MAX_PAYLOAD_CHARS, leaking
credentials and saturating logs.db with multi-MB repr() output. The fix
introduces spool-to-sidecar for oversized payloads (preserving audit
completeness) while keeping redaction always-on.
"""
import json
import os
from pathlib import Path

import pytest

from utils.logger.formatters import (
    _serialize_payload,
    _redact_secrets_in_string,
    _MAX_PAYLOAD_CHARS,
    _SPOOL_DIR,
    MaskableData,
    Base64Image,
)


class TestStringRedaction:
    def test_api_key_in_string_redacted(self):
        payload = {"config": "api_key=sk_live_abc123"}
        result = _serialize_payload(payload)
        assert "sk_live_abc123" not in str(result)
        assert "[REDACTED]" in str(result)

    def test_bearer_token_redacted(self):
        payload = {"header": "token=Bearer abc123def456"}
        result = _serialize_payload(payload)
        assert "abc123def456" not in str(result)

    def test_password_redacted(self):
        payload = {"db_url": "password=secret123"}
        result = _serialize_payload(payload)
        assert "secret123" not in str(result)

    def test_small_string_returned_inline(self):
        """Small strings (<= _MAX_PAYLOAD_CHARS) must be returned inline,
        NOT spooled."""
        small = "x" * 100
        result = _serialize_payload({"k": small})
        assert result == {"k": small}
        assert "SPOOLED" not in str(result)

    def test_large_string_spooled_not_truncated(self, tmp_path, monkeypatch):
        """Large strings (> _MAX_PAYLOAD_CHARS) must be SPOOLED to a sidecar
        file, NOT truncated. The full redacted payload must be recoverable
        from the spool file.

        This is the critical Pushback-5 regression: blind truncation destroys
        audit data. The spool-based approach preserves the full payload on
        disk while keeping the logs.db row small.
        """
        # Redirect the spool dir to tmp_path for test isolation.
        monkeypatch.setattr(
            "utils.logger.formatters._SPOOL_DIR",
            tmp_path / "log_spool",
        )

        large = "x" * (_MAX_PAYLOAD_CHARS + 5000)
        result = _serialize_payload({"k": large}, event_id="test_event_001")

        rendered = str(result)
        assert "SPOOLED" in rendered, (
            f"Expected SPOOL marker, got: {rendered[:200]}"
        )
        assert "test_event_001" in rendered, (
            "Spool filename should contain the event_id"
        )

        # The full payload must be recoverable from the spool file.
        spool_file = tmp_path / "log_spool" / "test_event_001.txt"
        assert spool_file.exists(), f"Spool file not created at {spool_file}"
        spooled_content = spool_file.read_text()
        assert len(spooled_content) == _MAX_PAYLOAD_CHARS + 5000, (
            f"Spooled content truncated: expected {_MAX_PAYLOAD_CHARS + 5000} "
            f"chars, got {len(spooled_content)}"
        )
        assert spooled_content == large, "Spooled content does not match original"


class TestReprFallbackRedaction:
    """The critical regression: repr() output MUST be redacted + spooled
    (NOT truncated)."""

    def test_unknown_object_repr_is_redacted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "utils.logger.formatters._SPOOL_DIR",
            tmp_path / "log_spool",
        )

        class SecretHolder:
            def __repr__(self):
                return "SecretHolder(api_key=sk_live_xyz789, password=hunter2)"

        payload = {"obj": SecretHolder()}
        result = _serialize_payload(payload, event_id="secret_test_001")
        rendered = str(result)
        assert "sk_live_xyz789" not in rendered
        assert "hunter2" not in rendered
        assert "[REDACTED]" in rendered

    def test_unknown_object_repr_large_is_spooled(self, tmp_path, monkeypatch):
        """A 50MB repr() must be spooled to disk, NOT truncated."""
        monkeypatch.setattr(
            "utils.logger.formatters._SPOOL_DIR",
            tmp_path / "log_spool",
        )

        class HugeRepr:
            def __repr__(self):
                return "x" * (_MAX_PAYLOAD_CHARS + 10000)

        payload = {"obj": HugeRepr()}
        result = _serialize_payload(payload, event_id="huge_repr_001")
        rendered = str(result)

        assert "SPOOLED" in rendered
        # The full repr must be on disk.
        spool_file = tmp_path / "log_spool" / "huge_repr_001.txt"
        assert spool_file.exists()
        spooled = spool_file.read_text()
        assert len(spooled) == _MAX_PAYLOAD_CHARS + 10000, (
            f"Spooled repr truncated: expected {_MAX_PAYLOAD_CHARS + 10000}, "
            f"got {len(spooled)}"
        )

    def test_repr_raising_returns_unserializable(self):
        class BrokenRepr:
            def __repr__(self):
                raise RuntimeError("repr failed")

        result = _serialize_payload({"obj": BrokenRepr()})
        assert result == {"obj": "<unserializable>"}

    def test_pydantic_v2_model_dump_invoked(self):
        """Pydantic v2 models must be serialized via model_dump() (not repr())."""
        pytest.importorskip("pydantic")
        from pydantic import BaseModel

        class MyModel(BaseModel):
            field_a: str = "value_a"
            field_b: int = 42

        result = _serialize_payload({"model": MyModel()})
        # model_dump() returns a dict; the dict is then recursed.
        assert isinstance(result, dict)
        assert isinstance(result["model"], dict)
        assert result["model"]["field_a"] == "value_a"
        assert result["model"]["field_b"] == 42
        # Verify repr() was NOT used (no "MyModel" in output).
        assert "MyModel" not in str(result)


class TestMaskableData:
    def test_base64_image_masked(self):
        img = Base64Image("iVBORw0KGgoAAAANSUhEUgAA..." * 100)
        result = _serialize_payload({"image": img})
        assert "[MASKED: Base64Image" in str(result)
        assert "iVBORw0KGgo" not in str(result)

    def test_bytes_masked(self):
        result = _serialize_payload({"blob": b"\x00\x01\x02" * 1000})
        assert "[MASKED: Binary Data" in str(result)

    def test_max_depth_enforced(self):
        nested = {
            "a": {
                "b": {
                    "c": {
                        "d": {
                            "e": {
                                "f": {
                                    "g": {
                                        "h": {
                                            "i": {
                                                "j": {"k": "deep"}
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        result = _serialize_payload(nested)
        assert "[MAX_DEPTH_EXCEEDED]" in str(result)
