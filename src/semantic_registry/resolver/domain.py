from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, Field, PrivateAttr

from src.semantic_registry.resolver.common import ExtractedTerm
from src.semantic_registry.yaml_schema.schemas import ConceptYaml, TermYaml


class DomainResult(BaseModel):
    domain: str | None = None
    confidence: float = 0.0
    candidates: list[str] = Field(default_factory=list)
    requires_clarification: bool = False

    _question: str = PrivateAttr(default="")

    @property
    def question(self) -> str:
        return self._question


class DomainDetector:
    def __init__(self, terms: list[TermYaml], concepts: list[ConceptYaml] | None = None) -> None:
        self.terms_by_name = {term.term: term for term in terms}
        self.concepts_by_name = {concept.concept: concept for concept in concepts or []}
        self.known_domains = sorted(
            {
                *[term.domain for term in terms if term.domain],
                *[concept.domain for concept in concepts or [] if concept.domain],
            }
        )

    def detect(
        self,
        question: str,
        extracted_terms: list[ExtractedTerm],
        explicit_domain: str | None = None,
    ) -> DomainResult:
        if explicit_domain:
            return self._with_question(
                DomainResult(
                    domain=explicit_domain,
                    confidence=1.0,
                    candidates=[explicit_domain],
                    requires_clarification=False,
                ),
                question,
            )

        question_domains = [domain for domain in self.known_domains if domain.lower() in question.lower()]
        if len(question_domains) == 1:
            return self._with_question(
                DomainResult(
                    domain=question_domains[0],
                    confidence=0.9,
                    candidates=question_domains,
                    requires_clarification=False,
                ),
                question,
            )
        if len(question_domains) > 1:
            return self._with_question(
                DomainResult(
                    domain=None,
                    confidence=0.5,
                    candidates=question_domains,
                    requires_clarification=True,
                ),
                question,
            )

        scores: dict[str, float] = defaultdict(float)
        for extracted in extracted_terms:
            term = self.terms_by_name.get(extracted.term)
            if term is None:
                continue

            default_domains = set(term.default_concept_by_domain)
            if len(default_domains) == 1:
                domain = next(iter(default_domains))
                scores[domain] += 2.0
                if term.domain:
                    scores[term.domain] += 1.0
            elif len(default_domains) > 1:
                for domain in default_domains:
                    scores[domain] += 0.75
            elif term.domain:
                scores[term.domain] += 1.0

            concept_domains = {
                self.concepts_by_name[concept].domain
                for concept in term.candidate_concepts
                if concept in self.concepts_by_name
            }
            if len(concept_domains) == 1:
                scores[next(iter(concept_domains))] += 1.5
            elif len(concept_domains) > 1:
                for domain in concept_domains:
                    scores[domain] += 0.5

        if not scores:
            return self._with_question(DomainResult(), question)

        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        top_score = ordered[0][1]
        top_domains = [domain for domain, score in ordered if score == top_score]
        candidates = [domain for domain, _ in ordered]
        if len(top_domains) > 1:
            return self._with_question(
                DomainResult(
                    domain=None,
                    confidence=min(0.8, top_score / sum(scores.values())),
                    candidates=top_domains,
                    requires_clarification=True,
                ),
                question,
            )

        return self._with_question(
            DomainResult(
                domain=ordered[0][0],
                confidence=min(0.95, top_score / sum(scores.values())),
                candidates=candidates,
                requires_clarification=False,
            ),
            question,
        )

    def _with_question(self, result: DomainResult, question: str) -> DomainResult:
        result._question = question
        return result
