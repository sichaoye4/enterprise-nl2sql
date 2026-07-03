from __future__ import annotations

from pydantic import BaseModel, Field

from src.semantic_registry.models import AmbiguityLevel
from src.semantic_registry.resolver.common import ExtractedTerm
from src.semantic_registry.yaml_schema.schemas import ConceptYaml, MetricYaml, TermYaml


class ResolvedTerm(BaseModel):
    term: str
    resolved_concept: str | None = None
    candidate_concepts: list[str] = Field(default_factory=list)
    is_ambiguous: bool = False
    ambiguity_reason: str | None = None


class ConceptResolver:
    def __init__(
        self,
        terms: list[TermYaml],
        concepts: list[ConceptYaml] | None = None,
        metrics: list[MetricYaml] | None = None,
    ) -> None:
        self.terms_by_name = {term.term: term for term in terms}
        self.concepts_by_name = {concept.concept: concept for concept in concepts or []}
        self.metrics_by_name = {metric.metric: metric for metric in metrics or []}

    def resolve(self, extracted_terms: list[ExtractedTerm], domain: str | None = None) -> list[ResolvedTerm]:
        resolved: list[ResolvedTerm] = []
        for extracted in extracted_terms:
            term = self.terms_by_name.get(extracted.term)
            if term is None:
                metric = self.metrics_by_name.get(extracted.term)
                if metric is not None:
                    resolved.append(
                        ResolvedTerm(
                            term=extracted.term,
                            resolved_concept=metric.concept,
                            candidate_concepts=[metric.concept],
                        )
                    )
                    continue
                concept = self.concepts_by_name.get(extracted.term)
                resolved.append(
                    ResolvedTerm(
                        term=extracted.term,
                        resolved_concept=concept.concept if concept else None,
                        candidate_concepts=[concept.concept] if concept else [],
                        is_ambiguous=concept is None,
                        ambiguity_reason=None if concept else "term_not_found",
                    )
                )
                continue

            candidate_concepts = list(dict.fromkeys(term.candidate_concepts))
            default = term.default_concept_by_domain.get(domain or "") if domain else None
            if term.ambiguity_level == AmbiguityLevel.high and domain is None and len(candidate_concepts) > 1:
                resolved.append(
                    ResolvedTerm(
                        term=term.term,
                        resolved_concept=None,
                        candidate_concepts=candidate_concepts,
                        is_ambiguous=True,
                        ambiguity_reason="high_ambiguity_requires_domain",
                    )
                )
            elif default:
                if default not in candidate_concepts:
                    candidate_concepts.append(default)
                resolved.append(
                    ResolvedTerm(
                        term=term.term,
                        resolved_concept=default,
                        candidate_concepts=candidate_concepts,
                    )
                )
            elif len(candidate_concepts) == 1:
                resolved.append(
                    ResolvedTerm(
                        term=term.term,
                        resolved_concept=candidate_concepts[0],
                        candidate_concepts=candidate_concepts,
                    )
                )
            elif candidate_concepts:
                resolved.append(
                    ResolvedTerm(
                        term=term.term,
                        resolved_concept=None,
                        candidate_concepts=candidate_concepts,
                        is_ambiguous=True,
                        ambiguity_reason="multiple_candidate_concepts",
                    )
                )
            else:
                resolved.append(
                    ResolvedTerm(
                        term=term.term,
                        resolved_concept=None,
                        candidate_concepts=[],
                        is_ambiguous=True,
                        ambiguity_reason="no_candidate_concepts",
                    )
                )
        return resolved
