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
from utils.source_context import SourceContextManager
from clients.llm import get_llm_client, LLMRequest
from tools.logger_agent.logger_prompts import ERROR_HEADER, LOGGER_AGENT_SYSTEM_PROMPT, trim_log_buffer
from utils.text_processing import escape_prompt_separators
import config

log = get_dual_logger(__name__)


async def run_tool_safely(tool: BaseTool, args: Dict[str, Any], telemetry: Any, **kwargs) -> ToolResult:
    """Execute a tool with centralized error handling and LLM-based diagnosis."""
    try:
        return await tool.execute(args, telemetry, **kwargs)
    except Exception as exc:
        raw_tb = traceback.format_exc()
        
        # Best-effort telemetry update
        try:
            await telemetry(tool.status("Tool failed. Diagnosing root cause...", "RUNNING"))
        except Exception:
            pass

        try:
            # Gather diagnostic context
            source_text = SourceContextManager.get_tool_sources(tool.name)
            from utils.logger.state import _tool_log_buffer
            buf = _tool_log_buffer.get() or []
            trimmed_logs = trim_log_buffer(
                buf,
                max_chars=getattr(config, 'LOGGER_AGENT_MAX_CONTEXT', 100_000),
            )

            user_content = (
                f"### Tool\n{tool.name}\n\n"
                f"### Traceback\n{escape_prompt_separators(raw_tb)}\n\n"
                f"### Logs\n{escape_prompt_separators(trimmed_logs)}\n\n"
                f"### Source Code\n{escape_prompt_separators(source_text)}\n###\n{{"
            )

            # Call LLM for diagnosis
            llm = get_llm_client(provider_type="azure")
            response = await llm.complete_chat(LLMRequest(
                messages=[
                    {"role": "system", "content": LOGGER_AGENT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                model="gpt-5.4-mini",
                response_format={"type": "json_object"},
            ))

            parsed = json.loads(response.content)
            user_msg = parsed.get("user_message", f"{ERROR_HEADER}\nAn unknown error occurred.")
            
            # Enforce header
            if not user_msg.startswith(ERROR_HEADER):
                user_msg = f"{ERROR_HEADER}\n{user_msg}"

            return ToolResult(output=user_msg, success=False, diagnosis=parsed)

        except Exception:
            # Hard fallback: raw traceback
            fallback = f"{ERROR_HEADER}\n\n{raw_tb}"
            return ToolResult(output=fallback, success=False)
        