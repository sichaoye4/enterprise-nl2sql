from __future__ import annotations

import os

from pydantic import BaseModel, Field
from sqlglot import exp

from src.semantic_registry.metadata.models import FanoutRisk, TableMetadata
from src.semantic_registry.metadata.provider import MetadataProvider
from src.semantic_registry.resolver.plan import SemanticQueryPlan
from src.semantic_registry.validation.parser import extract_tables, parse_sql


class SemanticCheckResult(BaseModel):
    name: str
    passed: bool
    message: str


class SemanticValidationResult(BaseModel):
    passed: bool
    checks: list[SemanticCheckResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class SemanticValidator:
    def __init__(self, *, dialect: str | None = None) -> None:
        self.dialect = dialect or os.getenv("SQL_DIALECT", "spark")

    def validate(
        self,
        sql: str,
        semantic_plan: SemanticQueryPlan,
        metadata_provider: MetadataProvider,
    ) -> SemanticValidationResult:
        checks: list[SemanticCheckResult] = []
        try:
            statement = parse_sql(sql, dialect=self.dialect)
        except Exception as exc:
            message = f"SQL parse failed: {exc}"
            return SemanticValidationResult(
                passed=False,
                checks=[SemanticCheckResult(name="parse", passed=False, message=message)],
                errors=[message],
            )

        table_names = extract_tables(statement)
        tables = [table for table_name in table_names if (table := self._get_table(metadata_provider, table_name)) is not None]
        metric_column = self._metric_column(tables, semantic_plan.metric)

        self._add(
            checks,
            "metric_column_matches_plan",
            semantic_plan.metric is None or metric_column is not None and self._column_used(statement, metric_column),
            "SQL must use the resolved metric column.",
        )
        self._add(
            checks,
            "time_semantic_matches_plan",
            self._uses_expected_time_column(statement, tables, semantic_plan.time_semantics),
            "SQL must use the physical column for the requested time semantic.",
        )
        self._add(
            checks,
            "allowed_dimensions",
            self._uses_allowed_dimensions(statement, tables, semantic_plan),
            "SQL uses only dimensions allowed for the selected metric.",
        )
        self._add(
            checks,
            "aggregation_matches_metric",
            self._aggregation_matches(statement, tables, semantic_plan.metric),
            "SQL aggregation must match the metric definition.",
        )
        self._add(
            checks,
            "no_metric_swap",
            not self._uses_different_metric(statement, tables, semantic_plan.metric),
            "SQL must not substitute a related but different metric.",
        )
        self._add(
            checks,
            "join_graph_and_fanout",
            self._join_graph_is_safe(table_names, semantic_plan.metric, metadata_provider),
            "SQL joins must respect documented join graph and fanout rules.",
        )
        self._add(
            checks,
            "grain_compatible",
            self._grain_compatible(statement, tables, semantic_plan),
            "SQL output grain must match requested dimensions.",
        )

        errors = [check.message for check in checks if not check.passed]
        return SemanticValidationResult(passed=not errors, checks=checks, errors=errors)

    def _add(self, checks: list[SemanticCheckResult], name: str, passed: bool, message: str) -> None:
        checks.append(SemanticCheckResult(name=name, passed=passed, message=message if not passed else "Passed."))

    def _get_table(self, metadata_provider: MetadataProvider, table_name: str) -> TableMetadata | None:
        return metadata_provider.get_table(table_name) or metadata_provider.get_table(table_name.rsplit(".", 1)[-1])

    def _metric_column(self, tables: list[TableMetadata], metric: str | None) -> str | None:
        if metric is None:
            return None
        for table in tables:
            for column in table.columns:
                if column.concept == metric:
                    return column.column_name
        return None

    def _column_used(self, statement: exp.Expression, column_name: str) -> bool:
        return any(column.name == column_name for column in statement.find_all(exp.Column))

    def _uses_expected_time_column(
        self,
        statement: exp.Expression,
        tables: list[TableMetadata],
        time_semantics: str | None,
    ) -> bool:
        if time_semantics is None:
            return True
        expected_columns = {
            column.column_name
            for table in tables
            for column in table.columns
            if column.concept == time_semantics or column.column_name == time_semantics
        }
        if not expected_columns:
            return True
        used_columns = {column.name for column in statement.find_all(exp.Column)}
        return bool(expected_columns & used_columns)

    def _uses_allowed_dimensions(
        self,
        statement: exp.Expression,
        tables: list[TableMetadata],
        semantic_plan: SemanticQueryPlan,
    ) -> bool:
        if semantic_plan.dimension is None:
            return True
        dimension_columns = {
            column.column_name
            for table in tables
            for column in table.columns
            if column.concept == semantic_plan.dimension or column.column_name == semantic_plan.dimension
        }
        if not dimension_columns:
            return True
        used_columns = {column.name for column in statement.find_all(exp.Column)}
        return bool(dimension_columns & used_columns)

    def _aggregation_matches(self, statement: exp.Expression, tables: list[TableMetadata], metric: str | None) -> bool:
        metric_columns = [
            column
            for table in tables
            for column in table.columns
            if metric is not None and column.concept == metric and column.aggregation
        ]
        if not metric_columns:
            return True
        for column in metric_columns:
            expected = self._normalize_aggregation(column.aggregation)
            for function in statement.find_all(exp.AggFunc):
                if self._function_contains_column(function, column.column_name):
                    return self._normalize_aggregation(function.key) == expected
        return False

    def _normalize_aggregation(self, value: str | None) -> str:
        normalized = (value or "").lower()
        if normalized in {"simple_sum"}:
            return "sum"
        if normalized in {"simple_count"}:
            return "count"
        if normalized in {"count_distinct", "distinct_count"}:
            return "count"
        return normalized

    def _function_contains_column(self, function: exp.Expression, column_name: str) -> bool:
        return any(column.name == column_name for column in function.find_all(exp.Column))

    def _uses_different_metric(self, statement: exp.Expression, tables: list[TableMetadata], metric: str | None) -> bool:
        used_columns = {column.name for column in statement.find_all(exp.Column)}
        for table in tables:
            for column in table.columns:
                is_metric = bool(column.aggregation)
                if is_metric and column.concept != metric and column.column_name in used_columns:
                    return True
        return False

    def _join_graph_is_safe(
        self,
        table_names: list[str],
        metric: str | None,
        metadata_provider: MetadataProvider,
    ) -> bool:
        if len(table_names) <= 1:
            return True
        join_paths = metadata_provider.get_join_paths(table_names)
        if not join_paths:
            return False
        for join_path in join_paths:
            if join_path.fanout_risk == FanoutRisk.high and metric not in join_path.safe_for_metrics:
                return False
        return True

    def _grain_compatible(
        self,
        statement: exp.Expression,
        tables: list[TableMetadata],
        semantic_plan: SemanticQueryPlan,
    ) -> bool:
        if semantic_plan.dimension is None:
            return True
        dimension_columns = {
            column.column_name
            for table in tables
            for column in table.columns
            if column.concept == semantic_plan.dimension or column.column_name == semantic_plan.dimension
        }
        if not dimension_columns:
            return True
        group = statement.args.get("group")
        if group is None:
            return True
        grouped_columns = {column.name for column in group.find_all(exp.Column)}
        return bool(dimension_columns & grouped_columns)
