from __future__ import annotations

from src.semantic_registry.metadata.models import ColumnMetadata, ExampleQuery, JoinPath, TableMetadata
from src.semantic_registry.metadata.provider import MetadataProvider
from src.semantic_registry.pipeline import PipelineContext, SQLCandidate
from src.semantic_registry.repair import RepairLoop
from src.semantic_registry.resolver.plan import SemanticQueryPlan
from src.semantic_registry.validation.orchestrator import SQLValidator


class DummyProvider(MetadataProvider):
    def __init__(self) -> None:
        self.tables = {
            "orders": TableMetadata(
                table_name="orders",
                partition_column="payment_dt",
                certified=True,
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


def test_repair_loop_produces_repaired_sql_for_semantic_errors() -> None:
    provider = DummyProvider()
    context = PipelineContext(
        question="show paid GMV by channel",
        semantic_plan=SemanticQueryPlan(metric="paid_gmv", dimension="channel", time_semantics="payment_date"),
        context_prompt="""
<generation_context>
{"semantic_plan":{"metric":"paid_gmv","dimension":"channel","time_semantics":"payment_date"},"physical_mapping":{"table":"orders","metric_column":"paid_gmv_amt","dimension_column":"channel","time_column":"payment_dt","aggregation":"sum"}}
</generation_context>
""",
        sql_candidates=[
            SQLCandidate(
                candidate_id="A",
                sql="SELECT channel, COUNT(paid_gmv_amt) AS paid_gmv FROM orders WHERE payment_dt IS NOT NULL GROUP BY channel",
                generation_strategy="test",
                confidence="medium",
                reasoning_summary="test",
                parse_success=True,
                validation_errors=["semantic.aggregation_matches_metric: SQL aggregation must match the metric definition."],
            )
        ],
    )

    repaired = RepairLoop(metadata_provider=provider).repair(context, SQLValidator())

    assert repaired == context.sql_candidates
    assert context.sql_candidates[0].repaired is True
    assert "SUM(paid_gmv_amt)" in context.sql_candidates[0].sql
    assert context.sql_candidates[0].validation_errors == []


def test_repair_loop_skips_parse_errors() -> None:
    provider = DummyProvider()
    candidate = SQLCandidate(
        candidate_id="A",
        sql="SELECT FROM",
        generation_strategy="test",
        confidence="low",
        reasoning_summary="test",
        parse_success=False,
        validation_errors=["static.parse: SQL parse failed"],
    )
    context = PipelineContext(
        question="show paid GMV",
        semantic_plan=SemanticQueryPlan(metric="paid_gmv"),
        sql_candidates=[candidate],
    )

    repaired = RepairLoop(metadata_provider=provider).repair(context, SQLValidator())

    assert repaired == []
    assert candidate.sql == "SELECT FROM"
    assert candidate.repair_attempted is False
