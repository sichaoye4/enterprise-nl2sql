from __future__ import annotations

import pytest

from src.semantic_registry.validation.static_validator import StaticValidator


@pytest.fixture
def validator() -> StaticValidator:
    return StaticValidator()


@pytest.fixture
def allowed_tables() -> set[str]:
    return {"orders"}


@pytest.fixture
def allowed_columns() -> dict[str, set[str]]:
    return {"orders": {"channel", "paid_gmv_amt", "payment_dt", "order_id"}}


def test_pass_for_valid_select_with_allowed_tables_and_columns(
    validator: StaticValidator,
    allowed_tables: set[str],
    allowed_columns: dict[str, set[str]],
) -> None:
    result = validator.validate(
        "SELECT channel, SUM(paid_gmv_amt) AS paid_gmv FROM orders WHERE payment_dt >= '2026-01-01' GROUP BY channel LIMIT 100",
        allowed_tables,
        allowed_columns,
    )

    assert result.passed is True


def test_fail_for_select_star(
    validator: StaticValidator,
    allowed_tables: set[str],
    allowed_columns: dict[str, set[str]],
) -> None:
    result = validator.validate("SELECT * FROM orders LIMIT 100", allowed_tables, allowed_columns)

    assert result.passed is False
    assert any(check.name == "no_select_star" and not check.passed for check in result.checks)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO orders SELECT order_id FROM orders",
        "DELETE FROM orders WHERE order_id = 1",
        "DROP TABLE orders",
    ],
)
def test_fail_for_write_or_ddl(
    validator: StaticValidator,
    allowed_tables: set[str],
    allowed_columns: dict[str, set[str]],
    sql: str,
) -> None:
    result = validator.validate(sql, allowed_tables, allowed_columns)

    assert result.passed is False
    assert any(check.name == "select_only" and not check.passed for check in result.checks)


def test_fail_for_unauthorized_table(validator: StaticValidator, allowed_columns: dict[str, set[str]]) -> None:
    result = validator.validate("SELECT paid_gmv_amt FROM payments LIMIT 100", {"orders"}, allowed_columns)

    assert result.passed is False
    assert any(check.name == "allowed_tables" and not check.passed for check in result.checks)


def test_fail_for_unauthorized_column(
    validator: StaticValidator,
    allowed_tables: set[str],
    allowed_columns: dict[str, set[str]],
) -> None:
    result = validator.validate("SELECT card_number FROM orders LIMIT 100", allowed_tables, allowed_columns)

    assert result.passed is False
    assert any(check.name == "allowed_columns" and not check.passed for check in result.checks)
