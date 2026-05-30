from __future__ import annotations

import pytest

from src.semantic_registry.validation.parser import (
    extract_columns,
    extract_tables,
    has_subqueries,
    is_select_only,
    parse_sql,
)


def test_parse_valid_select_sql() -> None:
    statement = parse_sql("SELECT order_id FROM orders")

    assert statement.sql().startswith("SELECT")


def test_parse_raises_on_invalid_sql() -> None:
    with pytest.raises(Exception):
        parse_sql("SELECT FROM")


def test_extract_tables_from_simple_query() -> None:
    statement = parse_sql("SELECT order_id FROM public.orders")

    assert extract_tables(statement) == ["public.orders"]


def test_extract_tables_from_join_query() -> None:
    statement = parse_sql("SELECT o.order_id FROM orders o JOIN users u ON o.user_id = u.user_id")

    assert extract_tables(statement) == ["orders", "users"]


def test_extract_columns_from_select() -> None:
    statement = parse_sql("SELECT o.order_id, o.paid_gmv_amt FROM orders o")

    assert extract_columns(statement) == ["o.order_id", "o.paid_gmv_amt"]


def test_is_select_only_with_insert_returns_false() -> None:
    statement = parse_sql("INSERT INTO orders SELECT * FROM source_orders")

    assert is_select_only(statement) is False


def test_has_subqueries_with_subquery_returns_true() -> None:
    statement = parse_sql("SELECT order_id FROM (SELECT order_id FROM orders) o")

    assert has_subqueries(statement) is True
