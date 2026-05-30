from __future__ import annotations

from src.semantic_registry.pipeline import SQLCandidate
from src.semantic_registry.repair import CandidateSelector


def candidate(candidate_id: str, errors: list[str], confidence: str = "medium") -> SQLCandidate:
    return SQLCandidate(
        candidate_id=candidate_id,
        sql="SELECT SUM(amount) AS amount FROM orders",
        generation_strategy="test",
        confidence=confidence,
        reasoning_summary="test",
        parse_success=not errors,
        validation_errors=errors,
    )


def test_selects_passing_candidate_over_failing_one() -> None:
    failing = candidate("A", ["semantic.metric_column_matches_plan: wrong metric"], confidence="high")
    passing = candidate("B", [], confidence="medium")

    selected = CandidateSelector().select([failing, passing])

    assert selected == passing


def test_returns_none_for_empty_list() -> None:
    assert CandidateSelector().select([]) is None
