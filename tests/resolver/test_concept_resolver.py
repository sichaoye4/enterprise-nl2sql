from __future__ import annotations

from src.semantic_registry.resolver import ConceptResolver, ExtractedTerm, MatchType


def extracted(term: str) -> ExtractedTerm:
    return ExtractedTerm(term=term, text=term, start_pos=0, end_pos=len(term), match_type=MatchType.exact)


def test_term_resolves_to_default_concept_when_domain_matches(resolver_terms, resolver_concepts, resolver_metrics) -> None:
    resolver = ConceptResolver(resolver_terms, resolver_concepts, resolver_metrics)

    resolved = resolver.resolve([extracted("revenue")], domain="finance")

    assert resolved[0].resolved_concept == "net_revenue"
    assert not resolved[0].is_ambiguous


def test_ambiguous_term_without_domain_is_flagged(resolver_terms, resolver_concepts, resolver_metrics) -> None:
    resolver = ConceptResolver(resolver_terms, resolver_concepts, resolver_metrics)

    resolved = resolver.resolve([extracted("revenue")])

    assert resolved[0].is_ambiguous
    assert resolved[0].candidate_concepts == ["gmv_concept", "paid_gmv", "net_revenue"]


def test_domain_specific_default_overrides_generic_candidates(resolver_terms, resolver_concepts, resolver_metrics) -> None:
    resolver = ConceptResolver(resolver_terms, resolver_concepts, resolver_metrics)

    resolved = resolver.resolve([extracted("revenue")], domain="commerce")

    assert resolved[0].resolved_concept == "paid_gmv"
