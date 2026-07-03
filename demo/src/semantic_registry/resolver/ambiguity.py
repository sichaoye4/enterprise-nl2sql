from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from src.semantic_registry.resolver.concept_resolver import ResolvedTerm
from src.semantic_registry.resolver.domain import DomainResult
from src.semantic_registry.resolver.common import normalize_tokens
from src.semantic_registry.yaml_schema.schemas import DimensionYaml, MetricYaml


class AmbiguityType(StrEnum):
    concept = "concept"
    metric = "metric"
    dimension = "dimension"
    time = "time"
    domain = "domain"


class Ambiguity(BaseModel):
    type: AmbiguityType
    term: str
    options: list[str] = Field(default_factory=list)
    question: str


class AmbiguityDetector:
    TIME_PATTERNS = {
        "last month": ["calendar_month", "fiscal_month", "trailing_30_days"],
        "this month": ["calendar_month", "fiscal_month"],
        "current month": ["calendar_month", "fiscal_month"],
        "last quarter": ["calendar_quarter", "fiscal_quarter"],
        "this quarter": ["calendar_quarter", "fiscal_quarter"],
        "current quarter": ["calendar_quarter", "fiscal_quarter"],
    }

    def __init__(self, metrics: list[MetricYaml] | None = None, dimensions: list[DimensionYaml] | None = None) -> None:
        self.metrics = metrics or []
        self.dimensions = dimensions or []
        self.metrics_by_concept: dict[str, list[MetricYaml]] = {}
        for metric in self.metrics:
            self.metrics_by_concept.setdefault(metric.concept, []).append(metric)

    def check(self, resolved_terms: list[ResolvedTerm], domain_result: DomainResult) -> list[Ambiguity]:
        question = getattr(domain_result, "question", "")
        ambiguities: list[Ambiguity] = []

        if domain_result.requires_clarification and len(domain_result.candidates) > 1:
            ambiguities.append(
                Ambiguity(
                    type=AmbiguityType.domain,
                    term="domain",
                    options=domain_result.candidates,
                    question="Which business domain should I use?",
                )
            )

        for resolved in resolved_terms:
            if resolved.is_ambiguous:
                ambiguities.append(
                    Ambiguity(
                        type=AmbiguityType.concept,
                        term=resolved.term,
                        options=resolved.candidate_concepts,
                        question=f"Which definition should I use for '{resolved.term}'?",
                    )
                )
                continue

            if not resolved.resolved_concept:
                continue

            candidate_metrics = self.metrics_by_concept.get(resolved.resolved_concept, [])
            if len(candidate_metrics) > 1:
                ambiguities.append(
                    Ambiguity(
                        type=AmbiguityType.metric,
                        term=resolved.term,
                        options=[metric.metric for metric in candidate_metrics],
                        question=f"Which metric should I use for '{resolved.term}'?",
                    )
                )
            elif len(candidate_metrics) == 1:
                metric = candidate_metrics[0]
                if self._requires_dimension(question, metric):
                    ambiguities.append(
                        Ambiguity(
                            type=AmbiguityType.dimension,
                            term=resolved.term,
                            options=metric.allowed_dimensions,
                            question=f"Which dimension should I group '{resolved.term}' by?",
                        )
                    )

        time_options = self._time_ambiguity_options(question)
        if time_options:
            ambiguities.append(
                Ambiguity(
                    type=AmbiguityType.time,
                    term="time",
                    options=time_options,
                    question="Which time convention should I use?",
                )
            )

        return ambiguities

    def _requires_dimension(self, question: str, metric: MetricYaml) -> bool:
        if not metric.allowed_dimensions:
            return False
        if not re.search(r"\b(by|per|grouped by|break(?:down)? by)\b", question, flags=re.IGNORECASE):
            return False
        question_tokens = set(normalize_tokens(question))
        return not any(self._dimension_mentioned(dimension, question_tokens) for dimension in metric.allowed_dimensions)

    def _dimension_mentioned(self, dimension_name: str, question_tokens: set[str]) -> bool:
        dimension = next((item for item in self.dimensions if item.dimension == dimension_name), None)
        names = [dimension_name]
        if dimension is not None:
            names.extend(dimension.synonyms)
        return any(set(normalize_tokens(name)).issubset(question_tokens) for name in names)

    def _time_ambiguity_options(self, question: str) -> list[str]:
        normalized = " ".join(normalize_tokens(question))
        if any(marker in normalized for marker in ("calendar", "fiscal", "rolling", "trailing")):
            return []
        for phrase, options in self.TIME_PATTERNS.items():
            if phrase in normalized:
                return options
        return []
