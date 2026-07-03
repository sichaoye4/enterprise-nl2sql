from __future__ import annotations

from pathlib import Path

from src.semantic_registry.models import AmbiguityLevel
from src.semantic_registry.resolver.ambiguity import Ambiguity, AmbiguityDetector
from src.semantic_registry.resolver.clarification import ClarificationBuilder, ClarificationResponse
from src.semantic_registry.resolver.common import ExtractedTerm, MatchType, normalize_tokens, tokenize_with_spans
from src.semantic_registry.resolver.concept_resolver import ConceptResolver, ResolvedTerm
from src.semantic_registry.resolver.domain import DomainDetector, DomainResult
from src.semantic_registry.resolver.extractor import TermExtractor
from src.semantic_registry.resolver.plan import SemanticPlanGenerator, SemanticQueryPlan
from src.semantic_registry.resolver.registry import SemanticRegistryData, load_semantic_registry
from src.semantic_registry.resolver.synonym import SynonymMatcher
from src.semantic_registry.retrieval.hybrid import HybridRetriever, ScoredCandidate


class LLMJudge:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key

    def ask_llm(self, question: str, candidates: list[str]) -> str | None:
        return ask_llm(question, candidates)


def ask_llm(question: str, candidates: list[str]) -> str | None:
    question_tokens = set(normalize_tokens(question))
    best_candidate: str | None = None
    best_score = 0
    for candidate in candidates:
        candidate_tokens = set(normalize_tokens(candidate))
        score = len(question_tokens & candidate_tokens)
        if score > best_score:
            best_candidate = candidate
            best_score = score
    return best_candidate or (candidates[0] if candidates else None)


class SemanticResolver:
    def __init__(
        self,
        registry_data: SemanticRegistryData,
        retriever: HybridRetriever | None = None,
        llm_judge: LLMJudge | None = None,
    ) -> None:
        self.registry_data = registry_data
        self.extractor = TermExtractor(registry_data.terms, registry_data.dimensions)
        self.synonym_matcher = SynonymMatcher()
        self.domain_detector = DomainDetector(registry_data.terms, registry_data.concepts)
        self.concept_resolver = ConceptResolver(registry_data.terms, registry_data.concepts, registry_data.metrics)
        self.ambiguity_detector = AmbiguityDetector(registry_data.metrics, registry_data.dimensions)
        self.plan_generator = SemanticPlanGenerator(registry_data.concepts, registry_data.metrics, registry_data.dimensions)
        self.retriever = retriever or HybridRetriever(
            embedding_service=None,
            metadata_provider=None,
            semantic_registry_data=registry_data.as_retrieval_data(),
        )
        self.llm_judge = llm_judge or LLMJudge()
        self.clarification_builder = ClarificationBuilder()
        self.last_trace: list[str] = []

    @classmethod
    def from_semantic_dir(cls, semantic_dir: str | Path) -> "SemanticResolver":
        return cls(load_semantic_registry(semantic_dir))

    def resolve(self, question: str, domain: str | None = None) -> SemanticQueryPlan:
        self.last_trace = []
        extracted_terms = self._extract_terms(question)
        resolvable_terms = self._resolvable_terms(extracted_terms)
        domain_result = self.domain_detector.detect(question, resolvable_terms, explicit_domain=domain)
        resolved_terms = self.concept_resolver.resolve(resolvable_terms, domain_result.domain or domain)
        if extracted_terms:
            self.last_trace.append("domain_rule")

        ambiguities = self.ambiguity_detector.check(resolved_terms, domain_result)
        if not self._has_definitive_resolution(resolved_terms, domain_result, ambiguities):
            if not ambiguities:
                retrieved_terms, retrieved_resolved = self._retrieve(question, domain_result.domain or domain)
                if retrieved_resolved:
                    extracted_terms = retrieved_terms
                    resolved_terms = retrieved_resolved
                    domain_result = self.domain_detector.detect(question, extracted_terms, explicit_domain=domain)
                    ambiguities = self.ambiguity_detector.check(resolved_terms, domain_result)

            if not self._has_definitive_resolution(resolved_terms, domain_result, ambiguities) and not ambiguities:
                judged_terms, judged_resolved = self._judge(question)
                if judged_resolved:
                    extracted_terms = judged_terms
                    resolved_terms = judged_resolved
                    domain_result = self.domain_detector.detect(question, extracted_terms, explicit_domain=domain)
                    ambiguities = self.ambiguity_detector.check(resolved_terms, domain_result)

        if ambiguities or not self._has_definitive_resolution(resolved_terms, domain_result, ambiguities):
            self.last_trace.append("ask_clarification")

        plan = self.plan_generator.generate(
            question=question,
            extracted_terms=extracted_terms,
            resolved_concepts=resolved_terms,
            domain=domain_result,
            time_context=None,
            ambiguities=ambiguities,
        )
        if plan.requires_clarification and plan.clarification_question is None:
            clarification = self.clarification_builder.build(ambiguities, domain_result)
            plan.clarification_question = clarification.message or "Can you clarify which metric or domain you mean?"
        if self._has_high_ambiguity_without_domain(extracted_terms, resolved_terms, domain_result):
            plan.requires_clarification = True
            if plan.clarification_question is None:
                clarification = self.clarification_builder.build(ambiguities, domain_result)
                plan.clarification_question = clarification.message or "Can you clarify which metric or domain you mean?"
        return plan

    def build_clarification(self, question: str, context: dict | None = None) -> ClarificationResponse:
        context = context or {}
        domain = context.get("domain")
        extracted_terms = self.extractor.extract(question)
        resolvable_terms = self._resolvable_terms(extracted_terms)
        domain_result = self.domain_detector.detect(question, resolvable_terms, explicit_domain=domain)
        resolved_terms = self.concept_resolver.resolve(resolvable_terms, domain_result.domain or domain)
        ambiguities = self.ambiguity_detector.check(resolved_terms, domain_result)
        return self.clarification_builder.build(ambiguities, domain_result)

    def _extract_terms(self, question: str) -> list[ExtractedTerm]:
        self.last_trace.append("exact_match")
        exact_terms = self.extractor.extract_exact(question)
        if self._resolvable_terms(exact_terms):
            return exact_terms

        self.last_trace.append("synonym_match")
        synonym_terms = self.extractor.extract_synonyms(question)
        if synonym_terms:
            return [*exact_terms, *synonym_terms]

        matches = self.synonym_matcher.match([token.normalized for token in tokenize_with_spans(question)], self.registry_data.terms)
        return [*exact_terms, *self._extracted_from_synonym_matches(question, matches)]

    def _resolvable_terms(self, extracted_terms: list[ExtractedTerm]) -> list[ExtractedTerm]:
        return [term for term in extracted_terms if term.match_type != MatchType.dimension]

    def _extracted_from_synonym_matches(
        self,
        question: str,
        matches: list[tuple[str, list, float]],
    ) -> list[ExtractedTerm]:
        extracted: list[ExtractedTerm] = []
        lower_question = question.lower()
        for matched_text, terms, _confidence in matches:
            start = lower_question.find(matched_text.lower())
            if start < 0:
                continue
            end = start + len(matched_text)
            for term in terms:
                extracted.append(
                    ExtractedTerm(
                        term=term.term,
                        text=question[start:end],
                        start_pos=start,
                        end_pos=end,
                        match_type=MatchType.synonym,
                    )
                )
        return extracted

    def _retrieve(self, question: str, domain: str | None) -> tuple[list[ExtractedTerm], list[ResolvedTerm]]:
        self.last_trace.append("embedding_retrieval")
        result = self.retriever.retrieve(question, domain=domain, top_k=5)
        candidate = self._best_retrieval_candidate(result.candidate_metrics, result.candidate_concepts)
        if candidate is None:
            return [], []
        extracted = [
            ExtractedTerm(
                term=candidate.name,
                text=candidate.name,
                start_pos=0,
                end_pos=0,
                match_type=MatchType.synonym,
            )
        ]
        resolved = self.concept_resolver.resolve(extracted, domain)
        return extracted, resolved

    def _best_retrieval_candidate(
        self,
        metric_candidates: list[ScoredCandidate],
        concept_candidates: list[ScoredCandidate],
    ) -> ScoredCandidate | None:
        candidates = [*metric_candidates, *concept_candidates]
        if not candidates:
            return None
        best = max(candidates, key=lambda candidate: candidate.score)
        return best if best.score > 0 else None

    def _judge(self, question: str) -> tuple[list[ExtractedTerm], list[ResolvedTerm]]:
        self.last_trace.append("llm_judge")
        candidates = [
            *[term.term for term in self.registry_data.terms],
            *[metric.metric for metric in self.registry_data.metrics],
            *[concept.concept for concept in self.registry_data.concepts],
        ]
        best = self.llm_judge.ask_llm(question, candidates)
        if not best:
            return [], []
        extracted = [
            ExtractedTerm(
                term=best,
                text=best,
                start_pos=0,
                end_pos=0,
                match_type=MatchType.synonym,
            )
        ]
        return extracted, self.concept_resolver.resolve(extracted)

    def _has_definitive_resolution(
        self,
        resolved_terms: list[ResolvedTerm],
        domain_result: DomainResult,
        ambiguities: list[Ambiguity],
    ) -> bool:
        if ambiguities or domain_result.requires_clarification:
            return False
        return any(resolved.resolved_concept and not resolved.is_ambiguous for resolved in resolved_terms)

    def _has_high_ambiguity_without_domain(
        self,
        extracted_terms: list[ExtractedTerm],
        resolved_terms: list[ResolvedTerm],
        domain_result: DomainResult,
    ) -> bool:
        if domain_result.domain is not None:
            return False
        ambiguous_terms = {resolved.term for resolved in resolved_terms if resolved.is_ambiguous}
        for extracted in extracted_terms:
            term = self.registry_data.term_by_name(extracted.term) if hasattr(self.registry_data, "term_by_name") else None
            if term is None:
                term = next((item for item in self.registry_data.terms if item.term == extracted.term), None)
            if (
                term is not None
                and term.ambiguity_level == AmbiguityLevel.high
                and len(term.candidate_concepts) > 1
                and term.term in ambiguous_terms
            ):
                return True
        return False
