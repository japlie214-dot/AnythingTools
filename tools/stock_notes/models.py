# tools/stock_notes/models.py
from typing import Optional, Any
from pydantic import BaseModel, Field

class StockNotesInput(BaseModel):
    command: str = Field(..., description="One of: discover, note, details")
    instructions: Any = Field(
        default_factory=dict, 
        description="JSON object or string containing parameters for the specific command. E.g., {'ticker': 'AAPL'}."
    )
