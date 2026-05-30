from __future__ import annotations

from src.semantic_registry.metadata.postgres_adapter import PostgresMetadataProvider


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class FakeConnection:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return FakeResult(self.results.pop(0))


def test_search_tables_queries_catalog_and_normalizes_results() -> None:
    connection = FakeConnection(
        [
            [
                {
                    "table_name": "public.orders",
                    "description": "Orders fact table.",
                    "domain": "sales",
                    "certified": True,
                    "grain": ["order_id"],
                    "partition_column": "order_date",
                    "owner": "analytics",
                    "caveats": [],
                    "pii_reviewed": True,
                }
            ],
            [{"column_name": "order_id", "data_type": "text", "description": "Order id.", "is_pii": False}],
            [],
        ]
    )
    provider = PostgresMetadataProvider(connection)

    tables = provider.search_tables("orders", domain="sales")

    assert len(tables) == 1
    assert tables[0].table_name == "public.orders"
    assert tables[0].columns[0].column_name == "order_id"
    assert connection.calls[0][1] == {"query": "orders", "domain": "sales"}


def test_get_table_returns_none_when_table_is_missing() -> None:
    provider = PostgresMetadataProvider(FakeConnection([[]]))

    assert provider.get_table("public.missing") is None
