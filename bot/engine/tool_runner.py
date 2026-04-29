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
    job_id = kwargs.get("job_id")
    try:
        return await tool.execute(args, telemetry, **kwargs)
    except Exception as exc:
        raw_tb = traceback.format_exc()
        error_msg = f"Tool Execution Failure: {str(exc)}\n\nTraceback:\n{raw_tb}"
        if job_id:
            try:
                from database.logs_writer import logs_enqueue_write
                from utils.id_generator import ULID
                from datetime import datetime, timezone
                logs_enqueue_write(
                    "INSERT INTO logs (id, job_id, tag, level, status_state, message, payload_json, event_id, error_json, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ULID.generate(), job_id, "ToolRunner:Error", "ERROR", None, error_msg, json.dumps({"traceback": raw_tb}), ULID.generate(), None, datetime.now(timezone.utc).isoformat())
                )
            except Exception as log_err:
                log.dual_log(tag="ToolRunner", message=f"Failed to log error to logs: {log_err}", level="WARNING")
        
        log.dual_log(tag="ToolRunner", message=f"Tool execution failed: {exc}", level="ERROR", payload={"job_id": job_id, "tool": tool.name})
        return ToolResult(output=error_msg, success=False)


async def run_tool_with_orchestrator(
    tool_name: str,
    args: Dict[str, Any],
    telemetry: Any,
    job_id: str,
    **kwargs,
) -> ToolResult:
    """Execute a tool through the orchestrator for SoM-aware context."""
    from bot.orchestrator_core.router import OrchestratorRouter
    from utils.browser_daemon import daemon_manager

    browser_daemon = None
    if tool_name in kwargs.get("som_tools", ["scraper", "browser_task"]):
        try:
            if daemon_manager.status.value == "READY":
                browser_daemon = daemon_manager
        except Exception:
            pass

    router = OrchestratorRouter(job_id)

    async def execute_tool(tn, ta, **kw):
        from tools.registry import REGISTRY
        tool_cls = REGISTRY.get_tool_class(tn)
        if not tool_cls:
            return ToolResult(output=f"Tool not found: {tn}", success=False)

        tool_instance = REGISTRY.create_tool_instance(tn)
        return await run_tool_safely(tool_instance, ta, telemetry, **kw)

    return await router.run(
        tool_name=tool_name,
        tool_args=args,
        tool_executor=execute_tool,
        browser_daemon=browser_daemon,
        job_id=job_id,
        **kwargs,
    )
        