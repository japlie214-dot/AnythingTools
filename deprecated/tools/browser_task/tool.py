# deprecated/tools/browser_task/tool.py
from typing import Any
from pydantic import BaseModel, Field
from tools.base import BaseTool
from bot.core.agent import UnifiedAgent

class BrowserTaskInput(BaseModel):
    task: str = Field(..., description="Description of the browser task to perform.")

class BrowserTaskTool(BaseTool):
    name = "browser_task"
    INPUT_MODEL = BrowserTaskInput
    
    def is_resumable(self, args: dict[str, Any]) -> bool:
        return True

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        job_id = kwargs.get("job_id")
        session_id = kwargs.get("session_id", "0")
        
        if not job_id:
            return "Error: job_id is required."
        
        agent = UnifiedAgent(
            job_id=job_id,
            session_id=session_id,
            initial_mode="Navigator"
        )
        
        try:
            result = await agent.run(telemetry, **args)
            return result.get("result", result.get("message", "Browser task complete."))
        except Exception as e:
            return f"### ❌ Browser Task Failed\n{str(e)}"
