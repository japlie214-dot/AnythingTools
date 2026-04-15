# tools/actions/system/files/tool.py
from __future__ import annotations
import asyncio
import os
import time
from typing import Any

import config
from tools.base import BaseTool, TelemetryCallback, ToolResult
from clients.llm import MIME_TYPE_MAP

_DOWNLOAD_DIR = "chrome_download"


def _safe_path(filename: str) -> str | None:
    """Resolve filename to an absolute path inside chrome_download/.
    Returns None if the resolved path escapes the directory (traversal guard)."""
    resolved = os.path.abspath(os.path.join(_DOWNLOAD_DIR, filename))
    if not resolved.startswith(os.path.abspath(_DOWNLOAD_DIR) + os.sep):
        return None
    return resolved


class FileListDownloadsTool(BaseTool):
    from bot.core.constants import TOOL_SYSTEM_FILE_LIST_DOWNLOADS
    name = TOOL_SYSTEM_FILE_LIST_DOWNLOADS

    async def run(self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs) -> str:
        if not os.path.exists(_DOWNLOAD_DIR):
            return f"Downloads directory '{_DOWNLOAD_DIR}' does not exist."

        deadline = time.monotonic() + config.FILE_DOWNLOAD_WAIT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            entries  = os.listdir(_DOWNLOAD_DIR)
            transient = [f for f in entries if f.endswith((".crdownload", ".tmp"))]
            if not transient:
                if not entries:
                    return "chrome_download/ is empty."
                lines = [
                    f"{f}  ({os.path.getsize(os.path.join(_DOWNLOAD_DIR, f))} bytes)"
                    for f in sorted(entries)
                ]
                return "\n".join(lines)
            await asyncio.sleep(2)

        return (
            f"Timeout ({config.FILE_DOWNLOAD_WAIT_TIMEOUT_SECONDS}s) waiting for "
            f"download to finish: {', '.join(transient)}"
        )


class FileReadDocumentTool(BaseTool):
    from bot.core.constants import TOOL_SYSTEM_FILE_READ_DOCUMENT
    name = TOOL_SYSTEM_FILE_READ_DOCUMENT

    async def run(self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs) -> str:
        # Never called: execute() is overridden below.  Satisfies abstract contract.
        return ""

    async def execute(
        self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs
    ) -> ToolResult:
        """Override BaseTool.execute() directly so the LLM-diagnosis wrapper is
        bypassed and attachment_path is set on the returned ToolResult."""
        filename = args.get("filename", "").strip()
        if not filename:
            return ToolResult(output="Error: 'filename' is required.", success=False)

        path = _safe_path(filename)
        if path is None:
            return ToolResult(output="Error: path traversal attempt blocked.", success=False)
        if not os.path.exists(path):
            return ToolResult(output=f"Error: '{filename}' not found in chrome_download/.", success=False)

        from clients.llm import MIME_TYPE_MAP
        _, ext = os.path.splitext(filename)
        if ext.lower() not in MIME_TYPE_MAP:
            return ToolResult(
                output=f"Error: Unsupported file type '{ext}'. "
                       f"Supported: {', '.join(MIME_TYPE_MAP.keys())}",
                success=False,
            )
        return ToolResult(output="File attached.", success=True, attachment_paths=[path], event_id=None)


class FileDeleteTool(BaseTool):
    from bot.core.constants import TOOL_SYSTEM_FILE_DELETE
    name = TOOL_SYSTEM_FILE_DELETE

    async def run(self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs) -> str:
        filename = args.get("filename", "").strip()
        if not filename:
            return "Error: 'filename' is required."
        path = _safe_path(filename)
        if path is None:
            return "Error: path traversal attempt blocked."
        if not os.path.exists(path):
            return f"Error: '{filename}' not found in chrome_download/."
        os.remove(path)
        return f"Deleted '{filename}'."
    