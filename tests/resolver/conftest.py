from __future__ import annotations

import pytest

from src.semantic_registry.models import AmbiguityLevel, MetricType, SemanticStatus
from src.semantic_registry.resolver.registry import SemanticRegistryData
from src.semantic_registry.yaml_schema.schemas import ConceptYaml, DimensionYaml, MeasureRef, MetricYaml, TermYaml


@pytest.fixture
def resolver_terms() -> list[TermYaml]:
    return [
        TermYaml(
            term="gmv",
            description="Gross merchandise value.",
            synonyms=["gross merchandise value"],
            candidate_concepts=["gmv_concept"],
            default_concept_by_domain={"commerce": "gmv_concept"},
            ambiguity_level=AmbiguityLevel.medium,
            owner="analytics",
            domain="commerce",
            status=SemanticStatus.certified,
        ),
        TermYaml(
            term="paid_gmv",
            description="Paid gross merchandise value.",
            synonyms=["paid sales"],
            candidate_concepts=["paid_gmv"],
            default_concept_by_domain={"commerce": "paid_gmv"},
            owner="analytics",
            domain="commerce",
            status=SemanticStatus.certified,
        ),
        TermYaml(
            term="revenue",
            description="Generic revenue term.",
            synonyms=["sales", "income"],
            candidate_concepts=["gmv_concept", "paid_gmv", "net_revenue"],
            default_concept_by_domain={"commerce": "paid_gmv", "finance": "net_revenue"},
            ambiguity_level=AmbiguityLevel.high,
            owner="analytics",
            domain="finance",
            status=SemanticStatus.certified,
        ),
        TermYaml(
            term="active_user",
            description="Active user.",
            synonyms=["active users", "au"],
            candidate_concepts=["active_user_concept"],
            default_concept_by_domain={"growth": "active_user_concept"},
            owner="analytics",
            domain="growth",
            status=SemanticStatus.certified,
        ),
        TermYaml(
            term="channel",
            description="Attribution channel.",
            synonyms=["traffic source"],
            candidate_concepts=["order_concept"],
            default_concept_by_domain={"commerce": "order_concept"},
            owner="analytics",
            domain="commerce",
            status=SemanticStatus.certified,
        ),
    ]


@pytest.fixture
def resolver_concepts() -> list[ConceptYaml]:
    return [
        ConceptYaml(
            concept="gmv_concept",
            display_name="Gross Merchandise Value",
            domain="commerce",
            definition="Submitted order value.",
            owner="analytics",
            canonical_metric="gmv",
            status=SemanticStatus.certified,
        ),
        ConceptYaml(
            concept="paid_gmv",
            display_name="Paid GMV",
            domain="commerce",
            definition="Successfully paid order value.",
            owner="analytics",
            canonical_metric="paid_gmv",
            status=SemanticStatus.certified,
        ),
        ConceptYaml(
            concept="net_revenue",
            display_name="Net Revenue",
            domain="finance",
            definition="Finance recognized revenue.",
            owner="analytics",
            canonical_metric="net_revenue",
            status=SemanticStatus.certified,
        ),
        ConceptYaml(
            concept="active_user_concept",
            display_name="Active User",
            domain="growth",
            definition="User with activity.",
            owner="analytics",
            canonical_metric="active_users",
            status=SemanticStatus.certified,
        ),
        ConceptYaml(
            concept="order_concept",
            display_name="Order",
            domain="commerce",
            definition="Submitted order.",
            owner="analytics",
            canonical_metric="order_count",
            status=SemanticStatus.certified,
        ),
    ]


@pytest.fixture
def resolver_metrics() -> list[MetricYaml]:
    return [
        MetricYaml(
            metric="paid_gmv",
            concept="paid_gmv",
            description="Paid GMV.",
            type=MetricType.simple_sum,
            measure=MeasureRef(table="orders", column="paid_gmv_amt"),
            aggregation="sum",
            unit="CNY",
            default_time_dimension="payment_date",
            physical_time_column="payment_dt",
            allowed_dimensions=["channel", "region"],
            owner="analytics",
            status=SemanticStatus.certified,
        ),
        MetricYaml(
            metric="gmv",
            concept="gmv_concept",
            description="GMV.",
            type=MetricType.simple_sum,
            measure=MeasureRef(table="orders", column="gmv_amt"),
            aggregation="sum",
            unit="CNY",
            default_time_dimension="order_date",
            physical_time_column="order_dt",
            allowed_dimensions=["channel", "region"],
            owner="analytics",
            status=SemanticStatus.certified,
        ),
        MetricYaml(
            metric="net_revenue",
            concept="net_revenue",
            description="Net revenue.",
            type=MetricType.simple_sum,
            measure=MeasureRef(table="finance", column="net_revenue_amt"),
            aggregation="sum",
            unit="CNY",
            default_time_dimension="settlement_date",
            physical_time_column="settlement_dt",
            allowed_dimensions=["channel", "region"],
            owner="analytics",
            status=SemanticStatus.certified,
        ),
        MetricYaml(
            metric="order_count",
            concept="order_concept",
            description="Orders.",
            type=MetricType.simple_count,
            measure=MeasureRef(table="orders", column="order_id"),
            aggregation="count",
            unit="orders",
            default_time_dimension="order_date",
            physical_time_column="order_dt",
            allowed_dimensions=["channel"],
            owner="analytics",
            status=SemanticStatus.certified,
        ),
    ]


@pytest.fixture
def resolver_dimensions() -> list[DimensionYaml]:
    return [
        DimensionYaml(
            dimension="channel",
            description="Attribution channel.",
            entity="channel",
            synonyms=["traffic source"],
            physical_mappings=[],
            status=SemanticStatus.certified,
        ),
        DimensionYaml(
            dimension="region",
            description="Region.",
            synonyms=["geo"],
            physical_mappings=[],
            status=SemanticStatus.certified,
        ),
    ]


@pytest.fixture
def registry_data(
    resolver_terms: list[TermYaml],
    resolver_concepts: list[ConceptYaml],
    resolver_metrics: list[MetricYaml],
    resolver_dimensions: list[DimensionYaml],
) -> SemanticRegistryData:
    return SemanticRegistryData(
        terms=resolver_terms,
        concepts=resolver_concepts,
        metrics=resolver_metrics,
        dimensions=resolver_dimensions,
    )
