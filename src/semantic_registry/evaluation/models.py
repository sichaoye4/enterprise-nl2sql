from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvalCase(BaseModel):
    case_id: str
    question: str
    domain: str | None = None
    difficulty: str
    expected_semantic_plan: dict[str, Any]
    gold_sql: str
    required_tables: list[str] = Field(default_factory=list)
    required_columns: list[str] = Field(default_factory=list)
    active: bool = True
    tags: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now_iso)


class CaseResult(BaseModel):
    case_id: str
    passed: bool
    errors: list[str] = Field(default_factory=list)
    generated_sql: str | None = None
    generated_plan: dict[str, Any] | None = None
    expected_plan: dict[str, Any]
    gold_sql: str
    comparison_details: dict[str, Any] = Field(default_factory=dict)


class EvalResult(BaseModel):
    total_cases: int
    passed: int
    failed: int
    success_rate: float
    case_results: list[CaseResult] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)

