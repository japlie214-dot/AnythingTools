"""
bot/capabilities/system_tools.py

System Tools for Unified Agent Self-Management.

These are internal, autonomous tools available to agent instances:
- system:initialize_checklist - Initialize a multi-step checklist
- system:complete_step - Mark a step as complete with output
- system:switch_mode - Explicit mode switching with validation

Each tool updates the execution_ledger and manages job_items for tracking.
"""

import json
from typing import List, Dict, Any, Optional

from utils.logger import get_dual_logger
from database.writer import enqueue_write
from utils.id_generator import ULID

log = get_dual_logger(__name__)


class SystemToolResult:
    """Result from a system tool execution."""
    
    def __init__(self, success: bool, message: str = None, data: Dict[str, Any] = None):
        self.success = success
        self.message = message or ""
        self.data = data or {}
        self.output = json.dumps({
            "success": success,
            "message": message,
            "data": data
        })


class InitializeChecklistTool:
    """
    system:initialize_checklist
    
    Initializes a multi-step checklist for the agent to follow.
    Creates job_items entries for each step.
    
    Args:
        steps: List of step descriptions or identifiers
        job_id: Current job identifier
        caller_id: Session identifier
    
    Returns:
        Success confirmation with step count
    """
    
    name = "system:initialize_checklist"
    
    def execute(self, args: Dict[str, Any]) -> SystemToolResult:
        try:
            steps = args.get("steps", [])
            job_id = args.get("job_id")
            caller_id = args.get("caller_id")
            
            if not steps:
                return SystemToolResult(False, "No steps provided")
            
            if not job_id or not caller_id:
                return SystemToolResult(False, "Missing job_id or caller_id")
            
            # Create job_items entries for each step
            for i, step in enumerate(steps):
                step_identifier = step if isinstance(step, str) else step.get("identifier", f"step_{i}")
                step_input = step if isinstance(step, dict) else {"description": step}
                
                enqueue_write(
                    "INSERT INTO job_items (item_id, job_id, step_identifier, status, input_data, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (ULID.generate(), job_id, step_identifier, "PENDING", json.dumps(step_input), ULID.generate())
                )
            
            # Record in execution ledger
            ledger_id = ULID.generate()
            content = f"INIT_CHECKLIST: {len(steps)} steps"
            metadata = {"steps": steps}
            
            enqueue_write(
                "INSERT INTO execution_ledger (ledger_id, job_id, caller_id, role, content, attachment_metadata, char_count, attachment_char_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ledger_id, job_id, caller_id, "system", content, json.dumps(metadata), len(content), 0)
            )
            
            return SystemToolResult(
                success=True,
                message=f"Checklist initialized with {len(steps)} steps",
                data={"step_count": len(steps), "steps": steps}
            )
            
        except Exception as e:
            log.dual_log(tag="SystemTool:Checklist:Init", message=str(e), level="ERROR")
            return SystemToolResult(False, f"Failed to initialize checklist: {e}")


class CompleteStepTool:
    """
    system:complete_step
    
    Mark a checklist step as complete and record its output.
    
    Args:
        step_identifier: Unique identifier for the step
        output_data: Results/data from step completion
        job_id: Current job identifier
        caller_id: Session identifier
    
    Returns:
        Success confirmation
    """
    
    name = "system:complete_step"
    
    def execute(self, args: Dict[str, Any]) -> SystemToolResult:
        try:
            step_identifier = args.get("step_identifier")
            output_data = args.get("output_data", {})
            job_id = args.get("job_id")
            caller_id = args.get("caller_id")
            
            if not step_identifier:
                return SystemToolResult(False, "Missing step_identifier")
            
            if not job_id or not caller_id:
                return SystemToolResult(False, "Missing job_id or caller_id")
            
            # Update job_items entry
            enqueue_write(
                "UPDATE job_items SET status = ?, output_data = ?, updated_at = ? WHERE job_id = ? AND step_identifier = ?",
                ("COMPLETED", json.dumps(output_data), ULID.generate(), job_id, step_identifier)
            )
            
            # Record in execution ledger
            ledger_id = ULID.generate()
            content = f"STEP_COMPLETE: {step_identifier}"
            metadata = {"output": output_data}
            
            enqueue_write(
                "INSERT INTO execution_ledger (ledger_id, job_id, caller_id, role, content, attachment_metadata, char_count, attachment_char_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ledger_id, job_id, caller_id, "system", content, json.dumps(metadata), len(content), 0)
            )
            
            return SystemToolResult(
                success=True,
                message=f"Step '{step_identifier}' completed",
                data={"step": step_identifier, "output": output_data}
            )
            
        except Exception as e:
            log.dual_log(tag="SystemTool:Step:Complete", message=str(e), level="ERROR")
            return SystemToolResult(False, f"Failed to complete step: {e}")


class SwitchModeTool:
    """
    system:switch_mode
    
    Explicitly switch to a different mode with validation.
    This is used when an agent determines it needs different capabilities.
    
    Args:
        target: Target mode name (e.g., "ANALYST", "EDITOR")
        reason: Why the switch is needed
        objective: New objective for the target mode
        job_id: Current job identifier
        caller_id: Session identifier
    
    Returns:
        Success confirmation
    """
    
    name = "system:switch_mode"
    
    def execute(self, args: Dict[str, Any]) -> SystemToolResult:
        try:
            from bot.core.modes import MODES
            
            target = args.get("target")
            reason = args.get("reason", "")
            objective = args.get("objective", "")
            job_id = args.get("job_id")
            caller_id = args.get("caller_id")
            
            if not target:
                return SystemToolResult(False, "Missing target mode")
            
            if not job_id or not caller_id:
                return SystemToolResult(False, "Missing job_id or caller_id")
            
            # Validate target mode
            target_mode = MODES.get(target)
            if not target_mode:
                return SystemToolResult(False, f"Mode definition not found: {target}")
            
            # Verify reason and objective are provided
            if not reason:
                return SystemToolResult(False, "Reason for mode switch is required")
            
            if not objective:
                return SystemToolResult(False, "Objective for new mode is required")
            
            # Record mode switch in execution ledger
            ledger_id = ULID.generate()
            content = f"MODE_SWITCH: {target_mode.name}\nREASON: {reason}\nOBJECTIVE: {objective}"
            metadata = {
                "from_mode": "CURRENT",  # Will be resolved by context
                "to_mode": target_mode.name,
                "reason": reason,
                "objective": objective
            }
            
            enqueue_write(
                "INSERT INTO execution_ledger (ledger_id, job_id, caller_id, role, content, attachment_metadata, char_count, attachment_char_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ledger_id, job_id, caller_id, "system", content, json.dumps(metadata), len(content), 0)
            )
            
            log.dual_log(
                tag="SystemTool:SwitchMode",
                message=f"Mode switch requested to {target_mode.name}",
                payload={"reason": reason, "objective": objective}
            )
            
            return SystemToolResult(
                success=True,
                message=f"Switched to mode {target_mode.name}",
                data={
                    "mode": target_mode.name,
                    "reason": reason,
                    "objective": objective,
                    "execution_type": target_mode.execution_type
                }
            )
            
        except Exception as e:
            log.dual_log(tag="SystemTool:SwitchMode", message=str(e), level="ERROR")
            return SystemToolResult(False, f"Failed to switch mode: {e}")


# Registry of system tools
SYSTEM_TOOLS = {
    InitializeChecklistTool.name: InitializeChecklistTool,
    CompleteStepTool.name: CompleteStepTool,
    SwitchModeTool.name: SwitchModeTool,
}


def get_system_tool(tool_name: str) -> Optional[SystemToolResult]:
    """Get a system tool instance by name."""
    tool_class = SYSTEM_TOOLS.get(tool_name)
    if tool_class:
        return tool_class()
    return None


def is_system_tool(tool_name: str) -> bool:
    """Check if a tool name is a system tool."""
    return tool_name in SYSTEM_TOOLS