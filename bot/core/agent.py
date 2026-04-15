"""bot/core/agent.py

Unified Agent State Machine.

Implements the Think -> Act -> Observe loop for autonomous execution,
50-tool-call hard cap enforcement, and mode switching logic.
"""

import json
from typing import Any, Dict
from bot.core.modes import MODES
from bot.core.weaver import build_session_context, get_session_cost
from bot.engine.tool_runner import run_tool_safely
from tools.registry import REGISTRY
from database.writer import append_to_ledger
from clients.llm import get_llm_client, LLMRequest
from utils.logger import get_dual_logger
from utils.logger.state import _current_job_id

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
        """Execute the agent loop with robust limits and Telegram notifications."""
        from database.writer import enqueue_write
        from database.connection import DatabaseManager
        import config
        
        # Ensure Logger knows which job this thread is handling
        _current_job_id.set(self.job_id)
        
        # Repetition tracking
        last_tool = None
        last_args_str = None
        repeat_count = 0
        
        # Use log.dual_log for the start message to ensure the Logger's
        # internal notification tier handles the thread/loop safety.
        log.dual_log(
            tag="Agent:Lifecycle",
            message=f"🚀 Job Started: {self.current_mode.name}",
            status_state="RUNNING",
            notify_user=True
        )

        while self.tool_call_count < 50:
            if self.current_mode.execution_type == "PROGRAMMATIC":
                tool_name = kwargs.get("tool_name")
                tool_instance = REGISTRY.create_tool_instance(tool_name)
                if tool_instance:
                    tool_result = await run_tool_safely(tool_instance, kwargs, telemetry, job_id=self.job_id, session_id=self.session_id, **kwargs)
                    return {"status": "COMPLETED" if tool_result.success else "FAILED", "result": tool_result.output}
                return {"status": "FAILED", "message": f"Programmatic tool {tool_name} not found."}

            # Budget check: 70% for history
            budget = getattr(config, "LLM_CONTEXT_CHAR_LIMIT", 100000)
            messages = build_session_context(self.session_id, self.current_mode.system_prompt, max_budget=budget)
            
            # Condensation trigger
            current_cost = get_session_cost(self.session_id, budget)
            if current_cost > budget * 0.7:
                log.dual_log(
                    tag="Agent:Condensation",
                    message="🗜️ Context Condensation Triggered (70% budget limit)",
                    notify_user=True
                )
                conn = DatabaseManager.get_read_connection()
                rows = conn.execute("SELECT id, role, content FROM execution_ledger WHERE session_id = ? AND role != 'system' ORDER BY id ASC", (self.session_id,)).fetchall()
                if len(rows) > 1:
                    mid = len(rows) // 2
                    old_rows = rows[:mid]
                    ids_to_delete = [str(r["id"]) for r in old_rows]
                    
                    if ids_to_delete:
                        history_text = "\n".join([f"{r['role']}: {r['content']}" for r in old_rows])
                        
                        summary_req = LLMRequest(
                            messages=[{"role": "system", "content": "Summarize this chronological history, preserving all key facts, extracted data, and tool results. Max 2000 chars."},
                                      {"role": "user", "content": history_text}],
                            max_tokens=800
                        )
                        summary_resp = await self.llm.complete_chat(summary_req)
                        
                        pl = ",".join(["?"] * len(ids_to_delete))
                        enqueue_write(f"DELETE FROM execution_ledger WHERE session_id = ? AND id IN ({pl})", (self.session_id, *ids_to_delete))
                        append_to_ledger(self.job_id, self.session_id, "system", f"<CONDENSED_HISTORY>\n{summary_resp.content}\n</CONDENSED_HISTORY>")
                        messages = build_session_context(self.session_id, self.current_mode.system_prompt, max_budget=budget)

            available_tools = REGISTRY.get_responses_tools(self.current_mode.allowed_tools)

            response = await self.llm.complete_chat(LLMRequest(
                messages=messages,
                tools=available_tools if available_tools else None
            ))

            if not response.tool_calls:
                content = response.content or ""
                append_to_ledger(self.job_id, self.session_id, "assistant", content)
                log.dual_log(
                    tag="Agent:Lifecycle",
                    message=f"✅ Job Completed",
                    notify_user=True,
                    payload={"content_preview": content[:200]}
                )
                return {"status": "COMPLETED", "result": content}

            for tool_call in response.tool_calls:
                self.tool_call_count += 1
                
                fn_name = tool_call["function"]["name"]
                args_str = tool_call["function"].get("arguments", "{}")
                
                # Repetition breaker
                if fn_name == last_tool and args_str == last_args_str:
                    repeat_count += 1
                    if repeat_count >= 3:
                        msg = "🚨 Security Intervention: Tool-retry loop detected"
                        log.dual_log(
                            tag="Agent:Security",
                            message=msg,
                            notify_user=True
                        )
                        append_to_ledger(self.job_id, self.session_id, "system", msg)
                        return {"status": "FAILED", "message": msg}
                else:
                    last_tool = fn_name
                    last_args_str = args_str
                    repeat_count = 0

                try:
                    args = json.loads(args_str)
                except Exception:
                    args = {}

                intent_content = f"Invoking tool {fn_name} with args {args_str}"
                append_to_ledger(self.job_id, self.session_id, "assistant", intent_content)
                log.dual_log(
                    tag="Agent:Action",
                    message=f"🛠️ Invoking {fn_name}",
                    payload={"args_preview": args_str[:500]},
                    notify_user=True
                )

                from bot.core.constants import TOOL_SYSTEM_SWITCH_MODE, TOOL_SYSTEM_DECLARE_FAILURE
                if fn_name == TOOL_SYSTEM_SWITCH_MODE:
                    target = args.get("target")
                    reason = args.get("reason")
                    objective = args.get("objective")
                    if target in MODES:
                        self.current_mode = MODES[target]
                        switch_msg = f"Mode switched to {target}. Reason: {reason}. Objective: {objective}."
                        append_to_ledger(self.job_id, self.session_id, "system", switch_msg)
                        log.dual_log(
                            tag="Agent:Lifecycle",
                            message=f"🔄 Mode Switched to {target}",
                            payload={"reason": reason},
                            notify_user=True
                        )
                        continue
                        
                if fn_name == TOOL_SYSTEM_DECLARE_FAILURE:
                    reason = args.get("reason", "No reason provided.")
                    msg = f"Agent explicitly declared failure: {reason}"
                    append_to_ledger(self.job_id, self.session_id, "system", msg)
                    log.dual_log(
                        tag="Agent:Lifecycle",
                        message=f"❌ Task Failed by Agent",
                        payload={"reason": reason},
                        notify_user=True
                    )
                    return {"status": "FAILED", "message": msg}

                tool_instance = REGISTRY.create_tool_instance(fn_name)
                if not tool_instance:
                    res_text = f"Error: Tool {fn_name} not found."
                else:
                    tool_result = await run_tool_safely(tool_instance, args, telemetry, job_id=self.job_id, session_id=self.session_id, **kwargs)
                    res_text = tool_result.output

                log.dual_log(
                    tag="Agent:Observation",
                    message=f"👀 {fn_name} completed",
                    payload={"output_length": len(res_text)},
                    notify_user=True
                )
                append_to_ledger(self.job_id, self.session_id, "tool", res_text)

        msg = "🚨 Security Intervention: Job force-terminated (50 tool calls exceeded)"
        log.dual_log(
            tag="Agent:Security",
            message=msg,
            notify_user=True
        )
        append_to_ledger(self.job_id, self.session_id, "system", msg)
        return {"status": "FAILED", "message": msg}
