from __future__ import annotations

from pydantic import BaseModel, Field


class LLMResponse(BaseModel):
    sql: str
    assumptions: list[str] = Field(default_factory=list)
    tables_used: list[str] = Field(default_factory=list)
    columns_used: list[str] = Field(default_factory=list)
    confidence: str
    reasoning_summary: str
    repair_attempted: bool = False
    repaired: bool = False
