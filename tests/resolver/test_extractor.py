from __future__ import annotations

from src.semantic_registry.resolver import MatchType, TermExtractor


def test_exact_term_extraction_from_simple_question(resolver_terms) -> None:
    terms = TermExtractor(resolver_terms).extract_exact("show gmv yesterday")

    assert [term.term for term in terms] == ["gmv"]
    assert terms[0].text == "gmv"
    assert terms[0].match_type == MatchType.exact


def test_multi_word_term_extraction(resolver_terms) -> None:
    terms = TermExtractor(resolver_terms).extract_exact("show me paid GMV by channel")

    assert [term.term for term in terms] == ["paid_gmv", "channel"]
    assert terms[0].text == "paid GMV"


def test_overlapping_terms_longest_match_wins(resolver_terms) -> None:
    terms = TermExtractor(resolver_terms).extract_exact("paid GMV")

    assert [term.term for term in terms] == ["paid_gmv"]


def test_case_insensitive_matching(resolver_terms) -> None:
    terms = TermExtractor(resolver_terms).extract_exact("Show ACTIVE User count")

    assert [term.term for term in terms] == ["active_user"]
    assert terms[0].text == "ACTIVE User"


def test_no_match_returns_empty_list(resolver_terms) -> None:
    assert TermExtractor(resolver_terms).extract("what happened yesterday") == []
