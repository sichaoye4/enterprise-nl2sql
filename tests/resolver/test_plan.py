from __future__ import annotations

from src.semantic_registry.resolver import ExtractedTerm, MatchType, ResolvedTerm, SemanticPlanGenerator


def extracted(term: str) -> ExtractedTerm:
    return ExtractedTerm(term=term, text=term, start_pos=0, end_pos=len(term), match_type=MatchType.exact)


def test_complete_plan_generation(resolver_concepts, resolver_metrics, resolver_dimensions) -> None:
    generator = SemanticPlanGenerator(resolver_concepts, resolver_metrics, resolver_dimensions)

    plan = generator.generate(
        "show paid GMV by channel for last 30 days",
        [extracted("paid_gmv")],
        [ResolvedTerm(term="paid_gmv", resolved_concept="paid_gmv", candidate_concepts=["paid_gmv"])],
        domain="commerce",
        time_context=None,
    )

    assert plan.metric == "paid_gmv"
    assert plan.dimension == "channel"
    assert plan.time_range == "last_30_days"
    assert plan.time_semantics == "payment_date"
    assert not plan.requires_clarification


def test_plan_requires_clarification_when_ambiguous(resolver_concepts, resolver_metrics, resolver_dimensions) -> None:
    generator = SemanticPlanGenerator(resolver_concepts, resolver_metrics, resolver_dimensions)

    plan = generator.generate(
        "show revenue",
        [extracted("revenue")],
        [
            ResolvedTerm(
                term="revenue",
                resolved_concept=None,
                candidate_concepts=["gmv_concept", "paid_gmv"],
                is_ambiguous=True,
                ambiguity_reason="multiple_candidate_concepts",
            )
        ],
        domain=None,
        time_context=None,
    )

    assert plan.requires_clarification
    assert plan.metric is None


def test_plan_json_output_format_matches_expected_structure(resolver_concepts, resolver_metrics, resolver_dimensions) -> None:
    generator = SemanticPlanGenerator(resolver_concepts, resolver_metrics, resolver_dimensions)

    plan = generator.generate(
        "show paid GMV",
        [extracted("paid_gmv")],
        [ResolvedTerm(term="paid_gmv", resolved_concept="paid_gmv", candidate_concepts=["paid_gmv"])],
        domain="commerce",
        time_context=None,
    )

    assert set(plan.model_dump()) == {
        "metric",
        "dimension",
        "time_range",
        "time_semantics",
        "domain",
        "filters",
        "requires_clarification",
        "clarification_question",
        "confidence",
    }
