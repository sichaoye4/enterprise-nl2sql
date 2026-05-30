from __future__ import annotations

from src.semantic_registry.resolver import (
    AmbiguityDetector,
    AmbiguityType,
    DomainResult,
    ResolvedTerm,
)


def test_concept_ambiguity_detection(resolver_metrics, resolver_dimensions) -> None:
    detector = AmbiguityDetector(resolver_metrics, resolver_dimensions)

    ambiguities = detector.check(
        [
            ResolvedTerm(
                term="revenue",
                resolved_concept=None,
                candidate_concepts=["gmv_concept", "paid_gmv"],
                is_ambiguous=True,
                ambiguity_reason="multiple_candidate_concepts",
            )
        ],
        DomainResult(domain=None, confidence=0.0, candidates=[], requires_clarification=False),
    )

    assert ambiguities[0].type == AmbiguityType.concept
    assert ambiguities[0].options == ["gmv_concept", "paid_gmv"]


def test_time_ambiguity_detection(resolver_metrics, resolver_dimensions) -> None:
    detector = AmbiguityDetector(resolver_metrics, resolver_dimensions)
    domain = DomainResult(domain="commerce", confidence=1.0, candidates=["commerce"], requires_clarification=False)
    domain._question = "show paid GMV last month"

    ambiguities = detector.check(
        [ResolvedTerm(term="paid_gmv", resolved_concept="paid_gmv", candidate_concepts=["paid_gmv"])],
        domain,
    )

    assert any(ambiguity.type == AmbiguityType.time for ambiguity in ambiguities)
