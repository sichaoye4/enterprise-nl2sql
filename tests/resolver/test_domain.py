from __future__ import annotations

from src.semantic_registry.resolver import DomainDetector, ExtractedTerm, MatchType


def extracted(term: str) -> ExtractedTerm:
    return ExtractedTerm(term=term, text=term, start_pos=0, end_pos=len(term), match_type=MatchType.exact)


def test_domain_detection_from_domain_specific_terms(resolver_terms, resolver_concepts) -> None:
    detector = DomainDetector(resolver_terms, resolver_concepts)

    result = detector.detect("show paid GMV", [extracted("paid_gmv")])

    assert result.domain == "commerce"
    assert not result.requires_clarification


def test_domain_detection_requires_clarification_when_ambiguous(resolver_terms, resolver_concepts) -> None:
    detector = DomainDetector(resolver_terms, resolver_concepts)

    result = detector.detect("show revenue", [extracted("revenue")])

    assert result.domain is None
    assert result.requires_clarification
    assert set(result.candidates) == {"commerce", "finance"}
