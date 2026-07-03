from __future__ import annotations

from typing import Any

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp


PLAN_FIELDS = ("metric", "dimension", "time_range", "time_semantics", "domain", "filters")


class ComparisonResult(BaseModel):
    exact_match: bool
    structurally_similar: bool
    differences: list[str] = Field(default_factory=list)


class PlanComparisonResult(BaseModel):
    exact_match: bool
    field_matches: dict[str, bool] = Field(default_factory=dict)
    differences: list[str] = Field(default_factory=list)


def compare_sql(generated: str, gold: str) -> ComparisonResult:
    generated_expr, generated_error = _parse(generated)
    gold_expr, gold_error = _parse(gold)
    differences: list[str] = []

    if generated_error:
        differences.append(f"generated_parse_error: {generated_error}")
    if gold_error:
        differences.append(f"gold_parse_error: {gold_error}")
    if generated_expr is None or gold_expr is None:
        return ComparisonResult(exact_match=False, structurally_similar=False, differences=differences)

    generated_canonical = _canonical_sql(generated_expr)
    gold_canonical = _canonical_sql(gold_expr)
    exact_match = generated_canonical == gold_canonical
    structurally_similar = exact_match or _select_structure(generated_expr) == _select_structure(gold_expr)

    if not exact_match:
        differences.extend(_expression_differences(generated_expr, gold_expr, generated_canonical, gold_canonical))
    if not structurally_similar and "SQL ASTs are structurally different." not in differences:
        differences.append("SQL ASTs are structurally different.")

    return ComparisonResult(
        exact_match=exact_match,
        structurally_similar=structurally_similar,
        differences=differences,
    )


def compare_plans(generated: dict[str, Any], expected: dict[str, Any]) -> PlanComparisonResult:
    field_matches: dict[str, bool] = {}
    differences: list[str] = []
    for field in PLAN_FIELDS:
        generated_value = _normalize_plan_value(generated.get(field))
        expected_value = _normalize_plan_value(expected.get(field))
        matches = generated_value == expected_value
        field_matches[field] = matches
        if not matches:
            differences.append(f"{field}: generated={generated_value!r}, expected={expected_value!r}")
    return PlanComparisonResult(
        exact_match=all(field_matches.values()),
        field_matches=field_matches,
        differences=differences,
    )


def _parse(sql: str) -> tuple[exp.Expression | None, str | None]:
    if not sql or not sql.strip():
        return None, "SQL is empty"
    try:
        return sqlglot.parse_one(sql), None
    except sqlglot.errors.ParseError as exc:
        return None, str(exc)


def _canonical_sql(expression: exp.Expression) -> str:
    return expression.sql(dialect="spark", normalize=True, pretty=False)


def _select_structure(expression: exp.Expression) -> dict[str, Any]:
    return {
        "type": expression.key,
        "tables": sorted(table.name for table in expression.find_all(exp.Table)),
        "columns": sorted(column.name for column in expression.find_all(exp.Column)),
        "aggregations": sorted(function.key.lower() for function in expression.find_all(exp.AggFunc)),
        "has_where": expression.args.get("where") is not None,
        "group": _canonical_sql(expression.args["group"]) if expression.args.get("group") else None,
        "order": _canonical_sql(expression.args["order"]) if expression.args.get("order") else None,
    }


def _expression_differences(
    generated_expr: exp.Expression,
    gold_expr: exp.Expression,
    generated_canonical: str,
    gold_canonical: str,
) -> list[str]:
    differences: list[str] = []
    generated_tables = sorted(table.name for table in generated_expr.find_all(exp.Table))
    gold_tables = sorted(table.name for table in gold_expr.find_all(exp.Table))
    generated_columns = sorted(column.name for column in generated_expr.find_all(exp.Column))
    gold_columns = sorted(column.name for column in gold_expr.find_all(exp.Column))

    if generated_tables != gold_tables:
        differences.append(f"tables differ: generated={generated_tables}, gold={gold_tables}")
    if generated_columns != gold_columns:
        differences.append(f"columns differ: generated={generated_columns}, gold={gold_columns}")
    if generated_canonical != gold_canonical:
        differences.append("canonical SQL differs")
    return differences


def _normalize_plan_value(value: Any) -> Any:
    if isinstance(value, list):
        return sorted((_normalize_plan_value(item) for item in value), key=repr)
    if isinstance(value, dict):
        return {key: _normalize_plan_value(value[key]) for key in sorted(value)}
    return value

