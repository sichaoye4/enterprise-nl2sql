from __future__ import annotations

from src.semantic_registry.evaluation.compare import compare_plans, compare_sql


def test_exact_sql_match_returns_exact_match_true() -> None:
    result = compare_sql("SELECT SUM(amount) AS total FROM orders", "SELECT SUM(amount) AS total FROM orders")

    assert result.exact_match is True
    assert result.structurally_similar is True
    assert result.differences == []


def test_different_sql_returns_exact_match_false() -> None:
    result = compare_sql("SELECT SUM(amount) AS total FROM orders", "SELECT COUNT(order_id) AS total FROM orders")

    assert result.exact_match is False
    assert result.structurally_similar is False
    assert result.differences


def test_plan_comparison_field_by_field() -> None:
    result = compare_plans(
        {
            "metric": "paid_gmv",
            "dimension": "channel",
            "time_range": "last_30_days",
            "time_semantics": "payment_date",
            "domain": "commerce",
            "filters": [],
        },
        {
            "metric": "paid_gmv",
            "dimension": "region",
            "time_range": "last_30_days",
            "time_semantics": "payment_date",
            "domain": "commerce",
            "filters": [],
        },
    )

    assert result.exact_match is False
    assert result.field_matches["metric"] is True
    assert result.field_matches["dimension"] is False

