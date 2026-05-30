from __future__ import annotations

from src.semantic_registry.metadata.models import ColumnMetadata, JoinPath, TableMetadata
from src.semantic_registry.models import JoinRelationship, MetricType
from src.semantic_registry.retrieval.documents import (
    RetrievalDoc,
    RetrievalDocType,
    generate_metric_doc,
    generate_table_doc,
    generate_term_doc,
)
from src.semantic_registry.yaml_schema.schemas import MeasureRef, MetricYaml, TermYaml


def test_generate_table_doc_includes_core_metadata() -> None:
    table = TableMetadata(
        table_name="public.orders",
        description="Orders fact table.",
        grain=["order_id"],
        partition_column="order_date",
        columns=[ColumnMetadata(column_name="amount", data_type="numeric", description="Order amount.")],
        join_paths=[
            JoinPath(
                from_table="public.orders",
                to_table="public.customers",
                relationship=JoinRelationship.many_to_one,
                join_condition="orders.customer_id = customers.customer_id",
            )
        ],
        caveats=["Refunds are excluded."],
    )

    doc = generate_table_doc(table)

    assert "public.orders" in doc
    assert "amount" in doc
    assert "Refunds are excluded." in doc


def test_generate_term_and_metric_docs() -> None:
    term = TermYaml(
        term="gmv",
        description="Gross merchandise value.",
        synonyms=["gross merchandise value"],
        candidate_concepts=["gmv_concept"],
        default_concept_by_domain={"finance": "gmv_concept"},
        owner="analytics",
        domain="finance",
    )
    metric = MetricYaml(
        metric="gmv",
        concept="gmv_concept",
        description="Gross merchandise value.",
        type=MetricType.simple_sum,
        measure=MeasureRef(table="orders", column="amount"),
        aggregation="sum",
        allowed_dimensions=["order_date"],
        owner="analytics",
    )

    assert "gross merchandise value" in generate_term_doc(term)
    assert "orders.amount" in generate_metric_doc(metric)


def test_retrieval_doc_content_hash_is_stable() -> None:
    doc = RetrievalDoc(id="table:orders", doc_type=RetrievalDocType.table, doc_name="orders", content="orders")

    assert len(doc.content_hash) == 64
    assert doc.content_hash == RetrievalDoc(
        id="table:orders",
        doc_type=RetrievalDocType.table,
        doc_name="orders",
        content="orders",
    ).content_hash
