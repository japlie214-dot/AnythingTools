"""tools/publisher/new_tool.py

Publisher Tool - Mode Initializer for Herald Mode.

Validates inputs and instantiates the Unified Agent in Herald mode.
"""

from typing import Any
from tools.base import BaseTool, TelemetryCallback
from bot.core.agent import UnifiedAgent


class PublisherTool(BaseTool):
    """
    Publisher Tool entry point. Instantiates the Unified Agent in Herald Mode.
    
    Input arguments:
        batch_id (str, required): The batch ID to publish
    """
    
    name = "publisher"
    
    def is_resumable(self, args: dict[str, Any]) -> bool:
        return False

    async def run(self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs) -> str:
        """Entry point for Publisher. Validates inputs and spawns Herald Agent."""
        
        # Extract required identifiers
        job_id = kwargs.get("job_id")
        chat_id = kwargs.get("chat_id")
        
        if not job_id:
            return "Error: job_id is required."
        
        if not chat_id:
            chat_id = 0
        
        # Extract arguments
        batch_id = args.get("batch_id")
        if not batch_id:
            return "Error: batch_id is required."
        
        # Normalize caller_id
        caller_id = str(chat_id)
        
        # Pass all args to agent
        agent_args = args.copy()
        
        # Instantiate Unified Agent in Herald mode
        agent = UnifiedAgent(
            job_id=job_id,
            caller_id=caller_id,
            initial_mode="Herald"
        )
        
        try:
            result = await agent.run(telemetry, **agent_args)
            return result.get("result", result.get("message", "Publisher execution complete."))
        except Exception as e:
            return f"### ❌ Publisher Failed\n{str(e)}"
        