"""tools/research/new_tool.py

Research Tool - Mode Initializer for Analyst Mode.

This file will replace the existing tool.py. It validates inputs and
instantiates the Unified Agent in Analyst mode.
"""

import os
from typing import Any
from tools.base import BaseTool, TelemetryCallback
from bot.core.agent import UnifiedAgent


class ResearchTool(BaseTool):
    """
    Research Tool entry point. Instantiates the Unified Agent in Analyst Mode.
    
    Input arguments:
        url (str, required): target URL to research
        goal (str, optional): research objective (defaults to institutional analysis)
    """
    
    name = "research"
    
    def is_resumable(self, args: dict[str, Any]) -> bool:
        """Returns True as research jobs can be resumed."""
        return True
    
    async def run(self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs) -> str:
        """Entry point for Research. Validates inputs and spawns Analyst Agent."""
        
        # Extract required identifiers
        job_id = kwargs.get("job_id")
        session_id = kwargs.get("session_id")
        
        if not job_id:
            return "Error: job_id is required."
        
        if not session_id:
            # Fallback for compatibility
            session_id = "0"
            
        # Extract and validate arguments
        url = args.get("url")
        goal = args.get("goal", "Comprehensive Institutional Analysis")
        
        if not url:
            return "Error: URL is required for the research tool."
        
        # Normalize caller_id as string
        caller_id = str(session_id)
        
        # Prepare arguments for agent (will be passed as kwargs)
        agent_args = {
            "url": url,
            "goal": goal
        }
        
        # Instantiate Unified Agent in Analyst mode
        agent = UnifiedAgent(
            job_id=job_id,
            session_id=caller_id,
            initial_mode="Analyst"
        )
        
        # Execute the agent
        try:
            result = await agent.run(telemetry, **agent_args)
            return result.get("result", result.get("message", "Research execution complete."))
        except Exception as e:
            return f"### ❌ Research Failed\n{str(e)}"
