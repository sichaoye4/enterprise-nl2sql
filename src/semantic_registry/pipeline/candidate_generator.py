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
        candidates = [self._generate("A", "direct", prompt, context)]
        # BIRD mode: skip plan-first candidate to save an API call
        if not self._is_bird_mode(context):
            candidates.append(self._generate("B", "plan_first", self._plan_first_prompt(prompt), context))
        return candidates

    @staticmethod
    def _is_bird_mode(context: "PipelineContext") -> bool:
        """Check if we're in BIRD benchmark mode (domain is a BIRD database)."""
        if not context.domain:
            return False
        from pathlib import Path
        db_root = Path(__file__).resolve().parent.parent.parent.parent / "bird_bench" / "dev" / "dev_20240627" / "databases" / "dev_databases"
        return (db_root / context.domain).exists()

    def _generate(
        self,
        candidate_id: str,
        strategy: str,
        prompt: str,
        context: "PipelineContext" | None = None,
    ) -> SQLCandidate:
        trace_stage = self._pending_trace_stage(context, candidate_id)
        try:
            response = self.llm_gateway.generate(prompt)
            self._record_trace_response(context, trace_stage, response.model_dump_json())
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
            self._record_trace_response(context, trace_stage, f"ERROR: {exc}")
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

    def _pending_trace_stage(self, context: "PipelineContext" | None, candidate_id: str) -> str | None:
        if context is None:
            return None
        suffix = f"candidate_{candidate_id.lower()}"
        for stage, entry in reversed(context.llm_trace.items()):
            if stage.endswith(suffix) and entry.get("response") is None:
                return stage
        return suffix

    def _record_trace_response(
        self,
        context: "PipelineContext" | None,
        stage: str | None,
        response: str,
    ) -> None:
        if context is None or stage is None:
            return
        if hasattr(context, "record_llm_trace"):
            context.record_llm_trace(stage, response=response)
