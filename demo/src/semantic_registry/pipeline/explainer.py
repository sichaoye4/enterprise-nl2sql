from __future__ import annotations

from pydantic import BaseModel, Field

from src.semantic_registry.pipeline.candidate_generator import SQLCandidate
from src.semantic_registry.resolver.plan import SemanticQueryPlan


class SQLExplanation(BaseModel):
    metric_used: str = ""
    metric_reason: str = ""
    table_selected: str = ""
    table_reason: str = ""
    columns_used: list[str] = Field(default_factory=list)
    time_range: str = ""
    time_semantics: str = ""
    assumptions: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class SQLExplainer:
    def explain(self, semantic_plan: SemanticQueryPlan, sql_candidate: SQLCandidate) -> SQLExplanation:
        metric = self._business_name(semantic_plan.metric)
        dimension = self._business_name(semantic_plan.dimension)
        time_semantics = self._business_name(semantic_plan.time_semantics)
        table = sql_candidate.tables_used[0] if sql_candidate.tables_used else ""
        columns = [value for value in (metric, dimension, time_semantics) if value]
        assumptions = list(sql_candidate.assumptions)
        if semantic_plan.filters:
            assumptions.append("Applied resolved semantic filters.")
        return SQLExplanation(
            metric_used=metric or "",
            metric_reason=self._metric_reason(metric, semantic_plan.domain),
            table_selected=table,
            table_reason=self._table_reason(table, metric),
            columns_used=columns,
            time_range=semantic_plan.time_range or "",
            time_semantics=time_semantics or "",
            assumptions=assumptions,
            caveats=sql_candidate.validation_errors,
        )

    def _metric_reason(self, metric: str | None, domain: str | None) -> str:
        if not metric:
            return "No resolved metric was available."
        if domain:
            return f"I interpreted the requested metric as {metric} because this query is in the {domain} domain."
        return f"I interpreted the requested metric as {metric} from the resolved semantic plan."

    def _table_reason(self, table: str, metric: str | None) -> str:
        if not table:
            return "No table was selected."
        if metric:
            return f"I selected this governed table because it contains the certified mapping for {metric}."
        return "I selected this governed table from the retrieved metadata context."

    def _business_name(self, value: str | None) -> str | None:
        if not value:
            return None
        words = []
        for part in value.split("_"):
            words.append(part.upper() if part.lower() in {"gmv", "pii", "id"} else part.capitalize())
        return " ".join(words)
