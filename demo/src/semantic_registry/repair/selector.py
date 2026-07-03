from __future__ import annotations

from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp

if TYPE_CHECKING:
    from src.semantic_registry.pipeline.candidate_generator import SQLCandidate


class CandidateSelector:
    def __init__(self) -> None:
        self.selection_log: list[dict[str, object]] = []

    def select(self, candidates: list[SQLCandidate]) -> SQLCandidate | None:
        self.selection_log = [self._candidate_log(candidate) for candidate in candidates]
        if not candidates:
            return None
        return max(candidates, key=self._score)

    def _score(self, candidate: SQLCandidate) -> tuple[int, float, int, float]:
        complexity = self._complexity(candidate.sql)
        confidence = self._confidence_value(candidate.confidence)
        return (
            1 if self._passes(candidate) else 0,
            confidence,
            -complexity,
            confidence,
        )

    def _candidate_log(self, candidate: SQLCandidate) -> dict[str, object]:
        return {
            "candidate_id": candidate.candidate_id,
            "passes_validation": self._passes(candidate),
            "confidence": candidate.confidence,
            "complexity": self._complexity(candidate.sql),
            "validation_errors": list(candidate.validation_errors),
        }

    def _passes(self, candidate: SQLCandidate) -> bool:
        return candidate.parse_success and not candidate.validation_errors

    def _confidence_value(self, confidence: str) -> float:
        return {"high": 1.0, "medium": 0.6, "low": 0.2}.get(confidence.lower(), 0.0)

    def _complexity(self, sql: str) -> int:
        try:
            statement = sqlglot.parse_one(sql)
        except Exception:
            return 10_000
        table_count = sum(1 for _ in statement.find_all(exp.Table))
        join_count = sum(1 for _ in statement.find_all(exp.Join))
        subquery_count = sum(1 for _ in statement.find_all(exp.Subquery))
        function_count = sum(1 for _ in statement.find_all(exp.Func))
        return table_count + join_count + subquery_count + function_count


__all__ = ["CandidateSelector"]
