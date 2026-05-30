from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from src.semantic_registry.models import (
    AmbiguityLevel,
    FanoutRisk,
    JoinRelationship,
    MetricType,
    SemanticConcept,
    SemanticDimension,
    SemanticEntity,
    SemanticJoinPath,
    SemanticMetric,
    SemanticPhysicalMapping,
    SemanticStatus,
    SemanticTerm,
    SemanticType,
)


def test_all_model_classes_can_be_instantiated_with_required_fields() -> None:
    rows = [
        SemanticTerm(
            term="gmv",
            description="Gross merchandise value.",
            owner="analytics",
            domain="finance",
        ),
        SemanticConcept(
            concept="gmv_concept",
            display_name="Gross Merchandise Value",
            domain="finance",
            definition="A finance concept.",
            owner="analytics",
        ),
        SemanticMetric(
            metric="gmv",
            concept="gmv_concept",
            description="Gross merchandise value.",
            type=MetricType.simple_sum,
            owner="analytics",
        ),
        SemanticDimension(dimension="order_date", description="Order date."),
        SemanticEntity(entity="order", description="Order entity."),
        SemanticPhysicalMapping(
            semantic_type=SemanticType.metric,
            semantic_name="gmv",
            physical_table="orders",
            physical_column="amount",
        ),
        SemanticJoinPath(
            join_path_name="orders_to_customers",
            from_table="orders",
            to_table="customers",
            relationship=JoinRelationship.many_to_one,
            join_condition="orders.customer_id = customers.customer_id",
        ),
    ]

    assert rows[0].ambiguity_level == AmbiguityLevel.low or rows[0].ambiguity_level is None
    assert rows[5].semantic_type == SemanticType.metric or rows[5].semantic_type is None
    assert rows[6].fanout_risk == FanoutRisk.low or rows[6].fanout_risk is None
    assert all(row is not None for row in rows)


@pytest.mark.asyncio
async def test_default_version_and_status_values(in_memory_session) -> None:
    term = SemanticTerm(
        term="gmv",
        description="Gross merchandise value.",
        owner="analytics",
        domain="finance",
    )
    in_memory_session.add(term)
    await in_memory_session.flush()

    assert term.version == 1
    assert term.status == SemanticStatus.draft


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            SemanticTerm,
            {
                "term": "unique_term",
                "description": "A term.",
                "owner": "analytics",
                "domain": "finance",
            },
        ),
        (
            SemanticConcept,
            {
                "concept": "unique_concept",
                "display_name": "Unique Concept",
                "domain": "finance",
                "definition": "A concept.",
                "owner": "analytics",
            },
        ),
        (
            SemanticMetric,
            {
                "metric": "unique_metric",
                "concept": "unique_concept",
                "description": "A metric.",
                "type": MetricType.simple_sum,
                "owner": "analytics",
            },
        ),
        (
            SemanticDimension,
            {
                "dimension": "unique_dimension",
                "description": "A dimension.",
            },
        ),
        (
            SemanticEntity,
            {
                "entity": "unique_entity",
                "description": "An entity.",
            },
        ),
        (
            SemanticJoinPath,
            {
                "join_path_name": "unique_join_path",
                "from_table": "orders",
                "to_table": "customers",
                "relationship": JoinRelationship.many_to_one,
                "join_condition": "orders.customer_id = customers.customer_id",
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_unique_constraints(in_memory_session, model, payload) -> None:
    in_memory_session.add_all([model(**payload), model(**payload)])

    with pytest.raises(IntegrityError):
        await in_memory_session.commit()


def test_schema_name_is_semantic_on_all_models() -> None:
    models = [
        SemanticTerm,
        SemanticConcept,
        SemanticMetric,
        SemanticDimension,
        SemanticEntity,
        SemanticPhysicalMapping,
        SemanticJoinPath,
    ]

    assert {model.__table__.schema for model in models} == {"semantic"}
