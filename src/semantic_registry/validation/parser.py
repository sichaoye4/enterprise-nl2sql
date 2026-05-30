from __future__ import annotations

import os
from collections.abc import Iterable

import sqlglot
from sqlglot import exp

if not hasattr(exp, "Statement"):
    setattr(exp, "Statement", exp.Expression)


def _dialect(dialect: str | None = None) -> str:
    return dialect or os.getenv("SQL_DIALECT", "spark")


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def parse_sql(sql: str, dialect: str | None = None) -> exp.Statement:
    return sqlglot.parse_one(sql, read=_dialect(dialect))


def _table_name(table: exp.Table) -> str:
    parts = [str(part) for part in (table.catalog, table.db, table.name) if part]
    return ".".join(parts)


def extract_tables(statement: exp.Expression) -> list[str]:
    return _unique(_table_name(table) for table in statement.find_all(exp.Table))


def extract_columns(statement: exp.Expression) -> list[str]:
    return _unique(column.sql(dialect=_dialect()) for column in statement.find_all(exp.Column))


def extract_functions(statement: exp.Expression) -> list[str]:
    names: list[str] = []
    for function in statement.find_all(exp.Func):
        name = function.name or function.key
        if name:
            names.append(name.lower())
    return _unique(names)


def extract_join_types(statement: exp.Expression) -> list[str]:
    join_types: list[str] = []
    for join in statement.find_all(exp.Join):
        parts = [
            str(join.args.get("side") or ""),
            str(join.args.get("kind") or "inner"),
        ]
        join_types.append(" ".join(part.lower() for part in parts if part).strip())
    return _unique(join_types)


def has_subqueries(statement: exp.Expression) -> bool:
    return any(True for _ in statement.find_all(exp.Subquery))


def has_aggregations(statement: exp.Expression) -> bool:
    return bool(statement.args.get("group")) or any(True for _ in statement.find_all(exp.AggFunc))


def is_select_only(statement: exp.Expression) -> bool:
    return isinstance(statement, exp.Select)
