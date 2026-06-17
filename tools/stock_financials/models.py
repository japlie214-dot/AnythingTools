# tools/stock_financials/models.py
from typing import Annotated, Literal, Union, Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator, model_validator

class ExtractInstructions(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    quarters: int = Field(default=8, ge=1, le=40)
    refresh: bool = Field(default=False)
    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, v: str) -> str: return v.upper().strip()

class QueryInstructions(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    statement_type: Literal["income", "balance", "cashflow"] = Field(default="income")
    concept: Optional[str] = None
    start_quarter: Optional[str] = None
    end_quarter: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=500)
    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, v: str) -> str: return v.upper().strip()
    @field_validator("concept")
    @classmethod
    def normalize_concept(cls, v: Optional[str]) -> Optional[str]:
        return v.replace(":", "_").strip() if v else v

class StatusInstructions(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, v: str) -> str: return v.upper().strip()

class CatalogInstructions(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    statement_type: Optional[Literal["income", "balance", "cashflow"]] = None
    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, v: str) -> str: return v.upper().strip()

InstructionsUnion = Annotated[
    Union[ExtractInstructions, QueryInstructions, StatusInstructions, CatalogInstructions],
    Field(discriminator="command")
]

class StockFinancialsInput(BaseModel):
    command: Literal["extract", "query", "status", "catalog"]
    instructions: Optional[Dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def handle_legacy_flat(cls, data: Any) -> Any:
        if not isinstance(data, dict): return data
        instructions = data.get("instructions") or {}
        legacy_keys = ["ticker", "quarters", "refresh", "statement_type", "concept", "start_quarter", "end_quarter", "limit"]
        for k in legacy_keys:
            if k in data and k not in instructions:
                instructions[k] = data[k]
        data["instructions"] = instructions
        return data

    def resolved_instructions(self):
        data = dict(self.instructions or {})
        if self.command == "extract": return ExtractInstructions.model_validate(data)
        if self.command == "query": return QueryInstructions.model_validate(data)
        if self.command == "status": return StatusInstructions.model_validate(data)
        if self.command == "catalog": return CatalogInstructions.model_validate(data)
        raise ValueError(f"Unknown command: {self.command}")

class SFFactRecord(BaseModel):
    ticker: str
    statement_type: str
    concept: str
    label: str
    quarter: str
    period_end: str
    fiscal_period: str
    fiscal_year: int
    numeric_value: Optional[float] = None
    unit: str = "USD"
    period_type: str = "duration"
    depth: int = 0
    is_total: int = 0
    concept_order: int = 0
    model_config = {"from_attributes": True}
