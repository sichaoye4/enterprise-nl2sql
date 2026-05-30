from __future__ import annotations

from src.semantic_registry.resolver import SynonymMatcher


def test_synonym_matching(resolver_terms) -> None:
    matches = SynonymMatcher().match(["show", "paid", "sales"], resolver_terms)

    assert matches[0][0] == "paid sales"
    assert [term.term for term in matches[0][1]] == ["paid_gmv"]
    assert matches[0][2] == 1.0


def test_partial_match_scoring(resolver_terms) -> None:
    matches = SynonymMatcher().match(["show", "gross", "merchandise"], resolver_terms)

    assert matches[0][0] == "gross merchandise"
    assert [term.term for term in matches[0][1]] == ["gmv"]
    assert matches[0][2] == 0.7
