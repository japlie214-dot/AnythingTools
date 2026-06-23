# bot/engine/tool_runner.py
"""Safe tool execution wrapper.

Centralizes error handling for tool executions. Per the agent-native sync
engine requirement, tools MUST raise Exception on failure (not return
markdown error strings). This wrapper lets exceptions propagate to the
worker's 3-strike crash-recovery logic.

HITL pauses do NOT use exceptions — they use hitl_registry.wait() thread
blocking, with the completion registry signaled BEFORE the block. See
tools/scraper/hitl.py.

Ref: https://docs.python.org/3/tutorial/errors.html#handling-exceptions
"""

from typing import Any, Dict
from tools.base import ToolResult, BaseTool
from utils.logger import get_dual_logger
from clients.llm import get_llm_client, LLMRequest
from utils.text_processing import escape_prompt_separators
import config

log = get_dual_logger(__name__)


async def run_tool_safely(tool: BaseTool, args: Dict[str, Any], telemetry: Any, **kwargs) -> ToolResult:
    """Execute a tool. Exceptions propagate to the worker.

    The worker's _run_job except block handles:
      - ToolError subclasses -> FAILED with the error message
      - Other Exception -> 3-strike crash recovery (INTERRUPTED / ABANDONED)

    We do NOT catch Exception here because that would mask crashes from
    the worker's crash-recovery logic. Per the requirement, tools must
    raise to crash into FAILED.

    Returns ToolResult(success=True) on normal tool return.
    """
    job_id = kwargs.get("job_id")
    # No try/except: let exceptions propagate.
    # The worker catches them and applies the 3-strike / FAILED logic.
    return await tool.execute(args, telemetry, **kwargs)


async def run_tool_with_orchestrator(
    tool_name: str,
    args: Dict[str, Any],
    telemetry: Any,
    job_id: str,
    **kwargs,
) -> ToolResult:
    """Execute a tool through the orchestrator for SoM-aware context.

    NOTE: This function has zero callers (grep-verified). It is retained
    for potential future use. The orchestrator path is not exercised by
    the sync engine.
    """
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
            # Raise instead of returning a failure ToolResult — per the
            # new contract, tools must raise on failure.
            from tools.base import ToolExecutionError
            raise ToolExecutionError(
                f"Tool not found: {tn}",
                tool_name=tn,
                job_id=job_id,
            )
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
