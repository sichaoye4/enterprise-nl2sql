from __future__ import annotations

from src.semantic_registry.metadata.models import ColumnMetadata, TableMetadata
from src.semantic_registry.metadata.provider import MetadataProvider
from src.semantic_registry.retrieval.hybrid import HybridRetriever, compute_keyword_match


class DummyEmbeddingService:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0] if "order" in text.lower() else [0.0, 1.0]


class DummyProvider(MetadataProvider):
    def search_tables(self, query: str, domain: str | None = None) -> list[TableMetadata]:
        return [
            TableMetadata(
                table_name="public.orders",
                description="Customer orders.",
                domain="sales",
                certified=True,
                grain=["order_id"],
                partition_column="order_date",
                owner="analytics",
                columns=[ColumnMetadata(column_name="amount", description="Order amount.")],
                caveats=["Refunds excluded."],
                usage_popularity=0.5,
            )
        ]

    def get_table(self, table_name: str) -> TableMetadata | None:
        return self.search_tables("")[0]

    def get_columns(self, table_name: str):
        return self.search_tables("")[0].columns

    def get_join_paths(self, tables: list[str]):
        return []

    def get_example_queries(self, query: str):
        return []


def test_keyword_match_uses_question_token_overlap() -> None:
    assert compute_keyword_match("order amount", "orders include amount and date") == 0.5


def test_hybrid_retriever_scores_tables_and_semantic_data() -> None:
    retriever = HybridRetriever(
        DummyEmbeddingService(),
        DummyProvider(),
        {
            "metrics": [
                {
                    "metric": "order_amount",
                    "description": "Order amount.",
                    "domain": "sales",
                    "status": "certified",
                }
            ],
            "dimensions": [{"dimension": "order_date", "description": "Order date.", "domain": "sales"}],
            "concepts": [{"concept": "orders", "definition": "Orders concept.", "domain": "sales"}],
        },
    )

    result = retriever.retrieve("order amount", domain="sales", top_k=3)

    assert result.candidate_tables[0].name == "public.orders"
    assert result.candidate_metrics[0].name == "order_amount"
    assert result.candidate_columns == ["public.orders.amount"]
    assert result.known_caveats == ["Refunds excluded."]
    assert "table:public.orders" in result.score_breakdown
