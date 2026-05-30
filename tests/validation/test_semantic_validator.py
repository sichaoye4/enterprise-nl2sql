from __future__ import annotations

from src.semantic_registry.metadata.models import ColumnMetadata, ExampleQuery, JoinPath, TableMetadata
from src.semantic_registry.metadata.provider import MetadataProvider
from src.semantic_registry.resolver.plan import SemanticQueryPlan
from src.semantic_registry.validation.semantic_validator import SemanticValidator


class DummyProvider(MetadataProvider):
    def __init__(self) -> None:
        self.tables = {
            "orders": TableMetadata(
                table_name="orders",
                columns=[
                    ColumnMetadata(column_name="paid_gmv_amt", concept="paid_gmv", aggregation="sum"),
                    ColumnMetadata(column_name="gmv_amt", concept="gmv", aggregation="sum"),
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


def test_pass_when_sql_matches_semantic_plan() -> None:
    result = SemanticValidator().validate(
        "SELECT channel, SUM(paid_gmv_amt) AS paid_gmv FROM orders WHERE payment_dt >= '2026-01-01' GROUP BY channel",
        SemanticQueryPlan(metric="paid_gmv", dimension="channel", time_semantics="payment_date"),
        DummyProvider(),
    )

    assert result.passed is True


def test_fail_when_wrong_metric_column_used() -> None:
    result = SemanticValidator().validate(
        "SELECT channel, SUM(gmv_amt) AS paid_gmv FROM orders WHERE payment_dt >= '2026-01-01' GROUP BY channel",
        SemanticQueryPlan(metric="paid_gmv", dimension="channel", time_semantics="payment_date"),
        DummyProvider(),
    )

    assert result.passed is False
    assert any(check.name == "metric_column_matches_plan" and not check.passed for check in result.checks)
