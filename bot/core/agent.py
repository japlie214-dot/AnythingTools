"""bot/core/agent.py

Unified Agent State Machine.

Implements the Think -> Act -> Observe loop for autonomous execution,
50-tool-call hard cap enforcement, and mode switching logic.
"""

import json
from typing import Any, Dict
from bot.core.modes import MODES
from bot.core.weaver import build_session_context
from bot.engine.tool_runner import run_tool_safely
from tools.registry import REGISTRY
from database.writer import append_to_ledger
from clients.llm import get_llm_client, LLMRequest
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


class UnifiedAgent:
    """Re-entrant state machine for autonomous agent execution."""
    
    def __init__(self, job_id: str, session_id: str, initial_mode: str):
        self.job_id = job_id
        self.session_id = session_id
        self.current_mode = MODES.get(initial_mode, MODES["Analyst"])
        self.tool_call_count = 0
        self.llm = get_llm_client("azure")

    async def run(self, telemetry: Any, **kwargs) -> Dict[str, Any]:
        """Execute the agent loop with 50-call hard cap."""
        while self.tool_call_count < 50:
            # PROGRAMMATIC modes execute the actual tool directly
            if self.current_mode.execution_type == "PROGRAMMATIC":
                tool_name = kwargs.get("tool_name")
                tool_instance = REGISTRY.create_tool_instance(tool_name)
                if tool_instance:
                    tool_result = await run_tool_safely(tool_instance, kwargs, telemetry, job_id=self.job_id, session_id=self.session_id, **kwargs)
                    return {"status": "COMPLETED" if tool_result.success else "FAILED", "result": tool_result.output}
                return {"status": "FAILED", "message": f"Programmatic tool {tool_name} not found."}

            messages = build_session_context(
                self.session_id,
                self.current_mode.system_prompt,
                max_budget=100000
            )
            
            # Prepare tool schemas for LLM using flat format required by Azure Responses API
            available_tools = REGISTRY.get_responses_tools(self.current_mode.allowed_tools)

            # ACT: Call LLM
            response = await self.llm.complete_chat(LLMRequest(
                messages=messages,
                tools=available_tools if available_tools else None
            ))

            # No tool calls = final response
            if not response.tool_calls:
                content = response.content or ""
                append_to_ledger(self.job_id, self.session_id, "assistant", content)
                return {"status": "COMPLETED", "result": content}

            # OBSERVE: Execute tool calls
            for tool_call in response.tool_calls:
                self.tool_call_count += 1
                
                fn_name = tool_call["function"]["name"]
                args_str = tool_call["function"].get("arguments", "{}")
                
                try:
                    args = json.loads(args_str)
                except Exception:
                    args = {}

                # Record assistant intent
                intent_content = f"Invoking tool {fn_name} with args {args_str}"
                append_to_ledger(self.job_id, self.session_id, "assistant", intent_content)

                # Handle mode switching
                from bot.core.constants import TOOL_SYSTEM_SWITCH_MODE
                if fn_name == TOOL_SYSTEM_SWITCH_MODE:
                    target = args.get("target")
                    reason = args.get("reason")
                    objective = args.get("objective")
                    if target in MODES:
                        self.current_mode = MODES[target]
                        switch_msg = f"Mode switched to {target}. Reason: {reason}. Objective: {objective}."
                        append_to_ledger(self.job_id, self.session_id, "system", switch_msg)
                        continue  # Skip to next iteration with new mode

                # Execute tool
                tool_instance = REGISTRY.create_tool_instance(fn_name)
                if not tool_instance:
                    res_text = f"Error: Tool {fn_name} not found."
                else:
                    tool_result = await run_tool_safely(tool_instance, args, telemetry, job_id=self.job_id, **kwargs)
                    res_text = tool_result.output

                # Record tool response
                append_to_ledger(self.job_id, self.session_id, "tool", res_text)

        # 50-call hard cap exceeded
        return {"status": "FAILED", "message": "Hard limit of 50 tool calls exceeded. Infinite loop aborted."}
