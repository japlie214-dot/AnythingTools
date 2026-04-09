# bot/telemetry.py
"""Temporary shim providing StatusUpdate class for tools.base compatibility.

Remove or refactor once tools.base fully migrates to new logging paradigm.
"""


class StatusUpdate:
    """Minimal shim that exposes status/message for telemetry (used by BaseTool)."""
    def __init__(self, message: str, status: str):
        self.message = message
        self.status = status

    def __str__(self):
        return f"[{self.status}] {self.message}"
