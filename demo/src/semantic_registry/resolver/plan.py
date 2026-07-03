from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from src.semantic_registry.resolver.ambiguity import Ambiguity
from src.semantic_registry.resolver.common import ExtractedTerm, normalize_tokens
from src.semantic_registry.resolver.concept_resolver import ResolvedTerm
from src.semantic_registry.resolver.domain import DomainResult
from src.semantic_registry.yaml_schema.schemas import ConceptYaml, DimensionYaml, MetricYaml


class SemanticQueryPlan(BaseModel):
    metric: str | None = None
    dimension: str | None = None
    time_range: str | None = None
    time_semantics: str | None = None
    domain: str | None = None
    filters: list[dict[str, Any]] = Field(default_factory=list)
    requires_clarification: bool = False
    clarification_question: str | None = None
    confidence: float = 0.0


class SemanticPlanGenerator:
    def __init__(
        self,
        concepts: list[ConceptYaml] | None = None,
        metrics: list[MetricYaml] | None = None,
        dimensions: list[DimensionYaml] | None = None,
    ) -> None:
        self.concepts_by_name = {concept.concept: concept for concept in concepts or []}
        self.metrics_by_name = {metric.metric: metric for metric in metrics or []}
        self.dimensions = dimensions or []
        self.metrics_by_concept: dict[str, list[MetricYaml]] = {}
        for metric in self.metrics_by_name.values():
            self.metrics_by_concept.setdefault(metric.concept, []).append(metric)

    def generate(
        self,
        question: str,
        extracted_terms: list[ExtractedTerm],
        resolved_concepts: list[ResolvedTerm],
        domain: str | DomainResult | None = None,
        time_context: dict[str, Any] | None = None,
        ambiguities: list[Ambiguity] | None = None,
    ) -> SemanticQueryPlan:
        metric = self._select_metric(question, resolved_concepts)
        dimension = self._select_dimension(question, metric)
        time_range = self._time_range(question, time_context)
        metric_model = self.metrics_by_name.get(metric or "")
        time_semantics = self._time_semantics(metric_model, time_context)
        domain_value = domain.domain if isinstance(domain, DomainResult) else domain
        ambiguity_list = ambiguities or []
        requires_clarification = (
            bool(ambiguity_list)
            or any(resolved.is_ambiguous for resolved in resolved_concepts)
            or metric is None
            or (isinstance(domain, DomainResult) and domain.requires_clarification)
        )
        clarification_question = self._clarification_question(ambiguity_list, metric)

        return SemanticQueryPlan(
            metric=metric,
            dimension=dimension,
            time_range=time_range,
            time_semantics=time_semantics,
            domain=domain_value,
            filters=[],
            requires_clarification=requires_clarification,
            clarification_question=clarification_question,
            confidence=self._confidence(metric, domain_value, requires_clarification, extracted_terms),
        )

    def _select_metric(self, question: str, resolved_terms: list[ResolvedTerm]) -> str | None:
        question_tokens = set(normalize_tokens(question))
        for resolved in resolved_terms:
            if resolved.is_ambiguous or not resolved.resolved_concept:
                continue
            if resolved.term in self.metrics_by_name:
                return resolved.term
            concept = self.concepts_by_name.get(resolved.resolved_concept)
            if concept and concept.canonical_metric:
                return concept.canonical_metric
            metrics = self.metrics_by_concept.get(resolved.resolved_concept, [])
            for metric in metrics:
                if set(normalize_tokens(metric.metric)).issubset(question_tokens):
                    return metric.metric
            if len(metrics) == 1:
                return metrics[0].metric
        return None

    def _select_dimension(self, question: str, metric: str | None) -> str | None:
        allowed = set(self.metrics_by_name[metric].allowed_dimensions) if metric in self.metrics_by_name else None
        for dimension in self.dimensions:
            if allowed is not None and dimension.dimension not in allowed:
                continue
            names = [dimension.dimension, *dimension.synonyms]
            if any(self._contains_name(question, name) for name in names):
                return dimension.dimension
        return None

    def _contains_name(self, question: str, name: str) -> bool:
        question_tokens = normalize_tokens(question)
        name_tokens = normalize_tokens(name)
        if not name_tokens:
            return False
        size = len(name_tokens)
        return any(question_tokens[index : index + size] == name_tokens for index in range(len(question_tokens) - size + 1))

    def _time_range(self, question: str, time_context: dict[str, Any] | None) -> str | None:
        if time_context and time_context.get("time_range"):
            return str(time_context["time_range"])
        normalized = " ".join(normalize_tokens(question))
        patterns = [
            (r"\blast\s+(\d+)\s+days\b", lambda match: f"last_{match.group(1)}_days"),
            (r"\bpast\s+(\d+)\s+days\b", lambda match: f"last_{match.group(1)}_days"),
            (r"\blast month\b", lambda _match: "last_month"),
            (r"\bthis month\b", lambda _match: "current_month"),
            (r"\bcurrent month\b", lambda _match: "current_month"),
            (r"\blast quarter\b", lambda _match: "last_quarter"),
            (r"\bthis quarter\b", lambda _match: "current_quarter"),
            (r"\bcurrent quarter\b", lambda _match: "current_quarter"),
            (r"\byesterday\b", lambda _match: "yesterday"),
            (r"\btoday\b", lambda _match: "today"),
        ]
        for pattern, builder in patterns:
            match = re.search(pattern, normalized)
            if match:
                return builder(match)
        return None

    def _time_semantics(self, metric: MetricYaml | None, time_context: dict[str, Any] | None) -> str | None:
        if time_context and time_context.get("time_semantics"):
            return str(time_context["time_semantics"])
        return metric.default_time_dimension if metric is not None else None

    def _clarification_question(self, ambiguities: list[Ambiguity], metric: str | None) -> str | None:
        if ambiguities:
            return ambiguities[0].question
        if metric is None:
            return "Which metric should I use?"
        return None

    def _confidence(
        self,
        metric: str | None,
        domain: str | None,
        requires_clarification: bool,
        extracted_terms: list[ExtractedTerm],
    ) -> float:
        if requires_clarification:
            return 0.35 if metric is None else 0.55
        confidence = 0.75
        if metric:
            confidence += 0.15
        if domain:
            confidence += 0.05
        if any(term.match_type.value == "synonym" for term in extracted_terms):
            confidence -= 0.05
        return max(0.0, min(1.0, confidence))
