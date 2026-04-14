"""tools/draft_editor/tool.py

Draft Editor Tool - Mode Initializer for Editor Mode.

Validates inputs and instantiates the Unified Agent in Editor mode.
"""

import json
from typing import Any
from pydantic import BaseModel, Field

from tools.base import BaseTool, TelemetryCallback
from bot.core.agent import UnifiedAgent


class DraftEditorInput(BaseModel):
    batch_id: str = Field(..., description="The unique ULID of the batch to edit.")
    operations: list = Field([], description="List of operations (ADD, REMOVE, SWAP, REORDER).")


class DraftEditorTool(BaseTool):
    """
    Draft Editor Tool entry point. Instantiates the Unified Agent in Editor Mode.
    
    Input arguments:
        batch_id (str, required): The batch ID to edit
        operations (list, optional): List of editing operations
    """
    
    name = "draft_editor"
    INPUT_MODEL = DraftEditorInput
    
    def is_resumable(self, args: dict[str, Any]) -> bool:
        return True

    async def run(self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs) -> str:
        """Entry point for Draft Editor. Validates inputs and spawns Editor Agent."""
        
        # Extract required identifiers
        job_id = kwargs.get("job_id")
        session_id = kwargs.get("session_id")
        
        if not job_id:
            return "Error: job_id is required."
        
        if not session_id:
            session_id = "0"
        
        # Extract arguments
        batch_id = args.get("batch_id")
        operations = args.get("operations", [])
        
        if not batch_id:
            return "Error: batch_id is required."
        
        # Normalize session_id
        session_id = str(session_id)
        
        # Pass all args to agent
        agent_args = args.copy()
        
        # Instantiate Unified Agent in Editor mode
        agent = UnifiedAgent(
            job_id=job_id,
            session_id=session_id,
            initial_mode="Editor"
        )
        
        try:
            result = await agent.run(telemetry, **agent_args)
            return result.get("result", result.get("message", "Draft Editor execution complete."))
        except Exception as e:
            return f"### ❌ Draft Editor Failed\n{str(e)}"
        