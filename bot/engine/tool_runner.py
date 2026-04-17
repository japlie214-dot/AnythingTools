"""bot/engine/tool_runner.py

Safe tool execution wrapper.

Centralizes error handling, exception catching, and error formatting 
for all tool executions. This replaces the error-handling logic 
previously embedded in BaseTool.execute().
"""

import traceback
import json
from typing import Any, Dict
from tools.base import ToolResult, BaseTool
from utils.logger import get_dual_logger
from clients.llm import get_llm_client, LLMRequest
from utils.text_processing import escape_prompt_separators
import config

log = get_dual_logger(__name__)


async def run_tool_safely(tool: BaseTool, args: Dict[str, Any], telemetry: Any, **kwargs) -> ToolResult:
    """Execute a tool with centralized error handling to return string-based tool results to the LLM."""
    try:
        return await tool.execute(args, telemetry, **kwargs)
    except Exception as exc:
        raw_tb = traceback.format_exc()
        error_msg = f"Tool Execution Failure: {str(exc)}\n\nTraceback:\n{raw_tb}"
        return ToolResult(output=error_msg, success=False)
        