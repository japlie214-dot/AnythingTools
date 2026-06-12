# tools/stock_notes/models.py
from typing import Optional, Any
from pydantic import BaseModel, Field

class StockNotesInput(BaseModel):
    # MANDATORY RULE FOR ALL DEVELOPERS:
    # All payloads for the stock_notes tool MUST be nested inside the "instructions" object.
    # Do NOT flatten parameters to the root level.
    command: str = Field(..., description="One of: discover, note, details")
    instructions: Any = Field(
        default_factory=dict,
        description="JSON object or string containing parameters for the specific command. E.g., {'ticker': 'AAPL', 'concept': 'us-gaap_Revenue'}."
    )
