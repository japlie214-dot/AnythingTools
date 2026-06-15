# tools/stock_financials/models.py
from typing import Any
from pydantic import BaseModel, Field

class StockFinancialsInput(BaseModel):
    command: str = Field(..., description="One of: extract, query, status")
    instructions: Any = Field(
        default_factory=dict,
        description="JSON object with command parameters. extract: {ticker, quarters, refresh}. query: {ticker, statement_type, concept, start_quarter, end_quarter, limit}."
    )
