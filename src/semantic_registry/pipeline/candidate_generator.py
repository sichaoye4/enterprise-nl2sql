from __future__ import annotations

from typing import TYPE_CHECKING

import sqlglot
from pydantic import BaseModel, Field

from src.semantic_registry.pipeline.llm_gateway import LLMGateway, validate_select_sql

if TYPE_CHECKING:
    from src.semantic_registry.pipeline.state_machine import PipelineContext


class SQLCandidate(BaseModel):
    candidate_id: str
    sql: str
    generation_strategy: str
    assumptions: list[str] = Field(default_factory=list)
    tables_used: list[str] = Field(default_factory=list)
    columns_used: list[str] = Field(default_factory=list)
    confidence: str
    reasoning_summary: str
    parse_success: bool
    validation_errors: list[str] = Field(default_factory=list)
    validation_results: dict | None = None
    repair_attempted: bool = False
    repaired: bool = False


class CandidateGenerator:
    def __init__(self, llm_gateway: LLMGateway | None = None) -> None:
        self.llm_gateway = llm_gateway or LLMGateway()

    def generate_candidates(self, context: "PipelineContext") -> list[SQLCandidate]:
        prompt = context.context_prompt or ""
        return [
            self._generate("A", "direct", prompt),
            self._generate("B", "plan_first", self._plan_first_prompt(prompt)),
        ]

    def _generate(self, candidate_id: str, strategy: str, prompt: str) -> SQLCandidate:
        try:
            response = self.llm_gateway.generate(prompt)
            validation_errors = self._validation_errors(response.sql)
            parse_success = not any(error.startswith("SQL parse error") for error in validation_errors)
            return SQLCandidate(
                candidate_id=candidate_id,
                sql=response.sql,
                generation_strategy=strategy,
                assumptions=response.assumptions,
                tables_used=response.tables_used,
                columns_used=response.columns_used,
                confidence=response.confidence,
                reasoning_summary=response.reasoning_summary,
                parse_success=parse_success,
                validation_errors=validation_errors,
            )
        except Exception as exc:
            return SQLCandidate(
                candidate_id=candidate_id,
                sql="",
                generation_strategy=strategy,
                assumptions=[],
                tables_used=[],
                columns_used=[],
                confidence="low",
                reasoning_summary="Generation failed before a valid SQL candidate was produced.",
                parse_success=False,
                validation_errors=[str(exc)],
            )

    def _validation_errors(self, sql: str) -> list[str]:
        errors = validate_select_sql(sql)
        if errors:
            return errors
        try:
            sqlglot.parse_one(sql)
        except sqlglot.errors.ParseError as exc:
            return [f"SQL parse error: {exc}"]
        return []

    def _plan_first_prompt(self, prompt: str) -> str:
        instruction = (
            "Plan first: identify the metric, table, dimension, time filter, and grouping steps. "
            "Then emit only the required JSON object with the final SQL."
        )
        return instruction + "\n\n" + prompt
