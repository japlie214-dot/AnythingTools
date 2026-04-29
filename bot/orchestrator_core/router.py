"""bot/orchestrator_core/router.py
Main orchestrator router for SoM-aware tool execution."""
from __future__ import annotations
from typing import Any, Callable, Awaitable
from utils.logger import get_dual_logger
from tools.base import ToolResult
from bot.orchestrator_core.context import SoMContextBuilder
from bot.orchestrator_core.eviction import BudgetEnforcer

log = get_dual_logger(__name__)

class OrchestratorRouter:
    def __init__(self, job_id: str, budget: int | None = None):
        self._job_id = job_id
        self._budget = budget

    async def run(self, tool_name: str, tool_args: dict[str, Any], tool_executor: Callable[..., Awaitable[ToolResult]], browser_daemon=None, **kwargs) -> ToolResult:
        try:
            context_builder = SoMContextBuilder(self._job_id)
            context_builder.initialize(tool_name, tool_args)

            if browser_daemon:
                try:
                    driver = browser_daemon.get_or_create_driver()
                    from utils.som_utils import wait_for_dom_stability, inject_som
                    from utils.observation_adapter import MarkingError
                    wait_for_dom_stability(driver)
                    try:
                        last_id = inject_som(driver, start_id=1)
                        if last_id > 1:
                            context_builder.inject_som_markers((0, last_id - 2))
                    except MarkingError:
                        browser_daemon.surgical_kill()
                        raise RuntimeError("SoM Injection hung. Browser killed.")
                except Exception as som_error:
                    log.dual_log(tag="Orchestrator:SoM:Error", message=f"SoM injection failed", level="WARNING", payload={"error": str(som_error)})

            context = context_builder.get_context()
            if context:
                kwargs["som_context"] = context.to_dict()
                kwargs["element_hints"] = context.element_hints

            result = await tool_executor(tool_name, tool_args, **kwargs)
            
            return result
        except Exception as error:
            log.dual_log(tag="Orchestrator:Error", message="Orchestrator error", level="ERROR", exc_info=error, payload={"error": str(error)})
            return ToolResult(output=f"Orchestrator error: {str(error)}", success=False)
        finally:
            if browser_daemon:
                try:
                    from utils.observation_adapter import BotasaurusObservationAdapter
                    BotasaurusObservationAdapter(browser_daemon.get_or_create_driver()).post_extract()
                except Exception:
                    pass
                browser_daemon.clear_job_tracking()
        