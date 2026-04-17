"""deprecated/tools/actions/actions/system/state/tool.py

System tools for checklist management and mode switching.
Provides autonomous agents with self-state management capabilities.
"""

import json
from typing import Any, List, Dict
from pydantic import BaseModel, Field

from tools.base import BaseTool
from typing import Any
from database.job_queue import add_job_item, update_item_status


class InitializeChecklistInput(BaseModel):
    steps: List[str] = Field(..., description="List of step identifiers to initialize.")


class InitializeChecklistTool(BaseTool):
    """Initialize a multi-step checklist in job_items."""
    from bot.core.constants import TOOL_SYSTEM_INITIALIZE_CHECKLIST
    name = TOOL_SYSTEM_INITIALIZE_CHECKLIST
    INPUT_MODEL = InitializeChecklistInput

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        job_id = kwargs.get("job_id")
        if not job_id:
            return "Error: No job_id found in context."
        
        steps = args.get("steps", [])
        for step in steps:
            # add_job_item safely ignores duplicates
            add_job_item(job_id, step, "{}")
        
        return f"Checklist initialized with {len(steps)} steps."


class CompleteStepInput(BaseModel):
    step_identifier: str = Field(..., description="The exact step name that was completed.")
    output_data: Dict[str, Any] = Field(..., description="Structured JSON data produced by this step.")


class CompleteStepTool(BaseTool):
    """Mark a step as completed with output data."""
    from bot.core.constants import TOOL_SYSTEM_COMPLETE_STEP
    name = TOOL_SYSTEM_COMPLETE_STEP
    INPUT_MODEL = CompleteStepInput

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        job_id = kwargs.get("job_id")
        if not job_id:
            return "Error: No job_id found in context."
        
        step = args.get("step_identifier")
        data = args.get("output_data", {})
        update_item_status(job_id, step, "COMPLETED", json.dumps(data))
        
        return f"Step '{step}' marked as COMPLETED."


class SwitchModeInput(BaseModel):
    target: str = Field(..., description="Target Mode (e.g., 'Scout', 'Analyst', 'Quant', 'Editor').")
    reason: str = Field(..., description="Natural language explanation of why the mode switch is necessary.")
    objective: str = Field(..., description="Explicit objective for the new mode.")


class SwitchModeTool(BaseTool):
    """Request a mode switch (handled by agent loop)."""
    from bot.core.constants import TOOL_SYSTEM_SWITCH_MODE
    name = TOOL_SYSTEM_SWITCH_MODE
    INPUT_MODEL = SwitchModeInput

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        # Note: This is a fallback. In practice, bot/core/agent.py intercepts
        # this tool call to perform the actual mode swap and ledger injection.
        target = args.get("target")
        reason = args.get("reason")
        objective = args.get("objective")
        
        return f"Mode switched to {target}. Reason: {reason}. Objective: {objective}."


# --- New: Declare Failure tool ---------------------------------------------
class DeclareFailureInput(BaseModel):
    reason: str = Field(..., description="Explanation of why the task cannot be completed.")


class DeclareFailureTool(BaseTool):
    """Declare that the task cannot be completed."""
    from bot.core.constants import TOOL_SYSTEM_DECLARE_FAILURE
    name = TOOL_SYSTEM_DECLARE_FAILURE
    INPUT_MODEL = DeclareFailureInput

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        # Intercepted by bot/core/agent.py directly.
        return f"Task failed: {args.get('reason')}"