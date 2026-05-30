from __future__ import annotations

import os

from sqlglot import exp

from src.semantic_registry.validation.parser import parse_sql


def inject_limit(sql: str, limit: int = 100) -> str:
    dialect = os.getenv("SQL_DIALECT", "spark")
    statement = parse_sql(sql, dialect=dialect)
    existing_limit = _existing_limit(statement)
    effective_limit = min(existing_limit, limit) if existing_limit is not None else limit
    statement.set("limit", exp.Limit(expression=exp.Literal.number(effective_limit)))
    return statement.sql(dialect=dialect)


def _existing_limit(statement: exp.Expression) -> int | None:
    limit_expression = statement.args.get("limit")
    if limit_expression is None or limit_expression.expression is None:
        return None
    try:
        return int(str(limit_expression.expression.this))
    except (TypeError, ValueError):
        return None
