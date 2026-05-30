from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.semantic_registry.pipeline.explainer import SQLExplanation
from src.semantic_registry.resolver.clarification import ClarificationResponse

if TYPE_CHECKING:
    from src.semantic_registry.pipeline.state_machine import PipelineContext


class PipelineResponse(BaseModel):
    query_id: str | None = None
    original_question: str
    semantic_interpretation: dict[str, Any] = Field(default_factory=dict)
    generated_sql: str
    explanation: SQLExplanation
    tables_used: list[str] = Field(default_factory=list)
    columns_used: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    validation_status: str
    validation_errors: list[str] = Field(default_factory=list)
    requires_clarification: bool
    clarification: ClarificationResponse | None = None
    error: str | None = None


class ResponseBuilder:
    def build(self, context: "PipelineContext") -> PipelineResponse:
        selected = context.selected_sql
        explanation = context.explanation or SQLExplanation()
        validation_errors = selected.validation_errors if selected else []
        return PipelineResponse(
            query_id=context.query_id,
            original_question=context.question,
            semantic_interpretation=self._semantic_interpretation(context),
            generated_sql=selected.sql if selected else "",
            explanation=explanation,
            tables_used=selected.tables_used if selected else [],
            columns_used=selected.columns_used if selected else [],
            assumptions=self._assumptions(selected, explanation),
            validation_status=self._validation_status(selected),
            validation_errors=validation_errors,
            requires_clarification=context.requires_clarification,
            clarification=context.clarification,
            error=context.error,
        )

    def _semantic_interpretation(self, context: "PipelineContext") -> dict[str, Any]:
        if context.semantic_plan is None:
            return {}
        return {
            "metric": context.semantic_plan.metric,
            "dimension": context.semantic_plan.dimension,
            "time": context.semantic_plan.time_range,
            "time_semantics": context.semantic_plan.time_semantics,
            "domain": context.semantic_plan.domain,
        }

    def _assumptions(self, selected: Any, explanation: SQLExplanation) -> list[str]:
        assumptions: list[str] = []
        if selected is not None:
            assumptions.extend(selected.assumptions)
        assumptions.extend(explanation.assumptions)
        deduped: list[str] = []
        for assumption in assumptions:
            if assumption not in deduped:
                deduped.append(assumption)
        return deduped

    def _validation_status(self, selected: Any) -> str:
        if selected is None:
            return "not_run"
        return "pass" if selected.parse_success and not selected.validation_errors else "fail"
