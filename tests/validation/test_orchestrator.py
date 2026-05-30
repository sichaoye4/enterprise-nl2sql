from __future__ import annotations

from src.semantic_registry.metadata.models import ColumnMetadata, ExampleQuery, JoinPath, TableMetadata
from src.semantic_registry.metadata.provider import MetadataProvider
from src.semantic_registry.resolver.plan import SemanticQueryPlan
from src.semantic_registry.validation.orchestrator import SQLValidator


class DummyProvider(MetadataProvider):
    def __init__(self) -> None:
        self.tables = {
            "orders": TableMetadata(
                table_name="orders",
                partition_column="payment_dt",
                columns=[
                    ColumnMetadata(column_name="paid_gmv_amt", concept="paid_gmv", aggregation="sum"),
                    ColumnMetadata(column_name="payment_dt", concept="payment_date"),
                    ColumnMetadata(column_name="channel", concept="channel"),
                ],
            )
        }

    def search_tables(self, query: str, domain: str | None = None) -> list[TableMetadata]:
        return list(self.tables.values())

    def get_table(self, table_name: str) -> TableMetadata | None:
        return self.tables.get(table_name)

    def get_columns(self, table_name: str) -> list[ColumnMetadata]:
        table = self.get_table(table_name)
        return table.columns if table else []

    def get_join_paths(self, tables: list[str]) -> list[JoinPath]:
        return []

    def get_example_queries(self, query: str) -> list[ExampleQuery]:
        return []


def test_full_validation_suite_returns_correct_result() -> None:
    result = SQLValidator().validate(
        "SELECT channel, SUM(paid_gmv_amt) AS paid_gmv FROM orders WHERE payment_dt >= '2026-01-01' GROUP BY channel",
        SemanticQueryPlan(metric="paid_gmv", dimension="channel", time_semantics="payment_date"),
        DummyProvider(),
        "analyst",
        {"orders"},
        {"orders": {"channel", "paid_gmv_amt", "payment_dt"}},
    )

    assert result.passed is True
    assert result.modified_sql is not None
    assert result.modified_sql.endswith("LIMIT 100")


def test_validation_suite_stops_at_first_failure_if_configured() -> None:
    result = SQLValidator(stop_on_first_failure=True).validate(
        "SELECT * FROM orders",
        SemanticQueryPlan(metric="paid_gmv", dimension="channel", time_semantics="payment_date"),
        DummyProvider(),
        "analyst",
        {"orders"},
        {"orders": {"channel", "paid_gmv_amt", "payment_dt"}},
    )

    assert result.passed is False
    assert any(check.name == "no_select_star" and not check.passed for check in result.static.checks)
    assert result.semantic.checks[0].name == "skipped"
