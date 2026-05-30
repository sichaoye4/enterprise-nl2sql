from __future__ import annotations

from src.semantic_registry.resolver import (
    Ambiguity,
    AmbiguityType,
    ClarificationBuilder,
    DomainResult,
)


def test_clarification_response_format() -> None:
    response = ClarificationBuilder().build(
        [
            Ambiguity(
                type=AmbiguityType.concept,
                term="revenue",
                options=["net_revenue", "paid_gmv"],
                question="Which revenue definition should I use?",
            )
        ],
        DomainResult(domain=None, confidence=0.0, candidates=[], requires_clarification=False),
    )

    assert response.needs_clarification
    assert "revenue" in response.message
    assert [option.value for option in response.options] == ["net_revenue", "paid_gmv"]


def test_response_does_not_contain_raw_table_names() -> None:
    response = ClarificationBuilder().build(
        [
            Ambiguity(
                type=AmbiguityType.concept,
                term="revenue",
                options=["net_revenue", "paid_gmv"],
                question="ads_order_channel_daily or ads_finance_channel_daily?",
            )
        ],
        DomainResult(domain=None, confidence=0.0, candidates=[], requires_clarification=False),
    )

    rendered = response.model_dump_json()
    assert "ads_order_channel_daily" not in rendered
    assert "ads_finance_channel_daily" not in rendered
