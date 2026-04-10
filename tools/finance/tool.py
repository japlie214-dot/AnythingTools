"""tools/finance/new_tool.py

Finance Tool - Mode Initializer for Quant Mode.

Validates inputs and instantiates the Unified Agent in Quant mode.
"""

from typing import Any
from tools.base import BaseTool, TelemetryCallback
from bot.core.agent import UnifiedAgent


class FinanceTool(BaseTool):
    """
    Finance Tool entry point. Instantiates the Unified Agent in Quant Mode.
    
    Input arguments:
        ticker (str, required): Stock ticker symbol
        action (str, optional): analyze, ingest, or query (default: analyze)
        statement (str, optional): Type of statements to retrieve
    """
    
    name = "finance"
    
    def is_resumable(self, args: dict[str, Any]) -> bool:
        """Returns True for ingest actions."""
        return args.get("action") == "ingest"
    
    async def run(self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs) -> str:
        """Entry point for Finance. Validates inputs and spawns Quant Agent."""
        
        # Extract required identifiers
        job_id = kwargs.get("job_id")
        chat_id = kwargs.get("chat_id")
        
        if not job_id:
            return "Error: job_id is required."
        
        if not chat_id:
            chat_id = 0
            
        # Extract and validate arguments
        ticker = args.get("ticker", "").strip().upper()
        if not ticker:
            return "Error: Ticker symbol is required."
        
        action = args.get("action", "analyze")
        statement = args.get("statement", "Quarterly Earnings")
        
        # Normalize caller_id
        caller_id = str(chat_id)
        
        # Pass all args to agent
        agent_args = args.copy()
        
        # Instantiate Unified Agent in Quant mode
        agent = UnifiedAgent(
            job_id=job_id,
            caller_id=caller_id,
            initial_mode="Quant"
        )
        
        try:
            result = await agent.run(telemetry, **agent_args)
            return result.get("result", result.get("message", "Finance execution complete."))
        except Exception as e:
            return f"### ❌ Finance Failed\n{str(e)}"