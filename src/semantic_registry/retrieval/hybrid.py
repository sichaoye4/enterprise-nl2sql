from __future__ import annotations

import math
import re
from typing import Any

from pydantic import BaseModel, Field

from src.semantic_registry.metadata.models import TableMetadata
from src.semantic_registry.metadata.provider import MetadataProvider
from src.semantic_registry.retrieval.documents import generate_table_doc
from src.semantic_registry.retrieval.embeddings import EmbeddingService


TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


class ScoredCandidate(BaseModel):
    name: str
    score: float
    description: str = ""
    domain: str = ""


class RetrievalResult(BaseModel):
    candidate_concepts: list[ScoredCandidate] = Field(default_factory=list)
    candidate_metrics: list[ScoredCandidate] = Field(default_factory=list)
    candidate_dimensions: list[ScoredCandidate] = Field(default_factory=list)
    candidate_tables: list[ScoredCandidate] = Field(default_factory=list)
    candidate_columns: list[str] = Field(default_factory=list)
    known_caveats: list[str] = Field(default_factory=list)
    score_breakdown: dict[str, dict[str, float]] = Field(default_factory=dict)


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(text)}


def compute_keyword_match(question: str, doc: str) -> float:
    question_tokens = _tokens(question)
    if not question_tokens:
        return 0.0
    doc_tokens = _tokens(doc)
    return len(question_tokens & doc_tokens) / len(question_tokens)


def compute_certification_boost(certified: bool) -> float:
    return 1.0 if certified else 0.0


def _value(candidate: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(candidate, dict) and name in candidate:
            return candidate[name]
        if hasattr(candidate, name):
            value = getattr(candidate, name)
            if value is not None:
                return value
    return default


def domain_filter(candidates: list[Any], domain: str | None) -> list[Any]:
    if domain is None:
        return candidates
    return [candidate for candidate in candidates if _value(candidate, "domain", default=None) in (None, "", domain)]


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    left = left[:size]
    right = right[:size]
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(0.0, min(1.0, numerator / (left_norm * right_norm)))


def _usage_popularity(candidate: Any) -> float:
    raw = _value(candidate, "usage_popularity", "usage_score", "popularity", "usage_count", default=0.0)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value > 1.0:
        value = value / 100.0
    return max(0.0, min(1.0, value))


class HybridRetriever:
    def __init__(
        self,
        embedding_service: EmbeddingService | None,
        metadata_provider: MetadataProvider | None,
        semantic_registry_data: dict[str, list[Any]] | None = None,
    ) -> None:
        self.embedding_service = embedding_service
        self.metadata_provider = metadata_provider
        self.semantic_registry_data = semantic_registry_data or {}

    def retrieve(self, question: str, domain: str | None = None, top_k: int = 10) -> RetrievalResult:
        result = RetrievalResult()
        question_embedding = self._embed_question(question)

        tables = self.metadata_provider.search_tables(question, domain=domain) if self.metadata_provider else []
        tables = domain_filter(tables, domain)
        result.candidate_tables = self._score_tables(question, question_embedding, tables, top_k, result)
        result.candidate_columns = self._candidate_columns(tables, top_k)
        result.known_caveats = sorted({caveat for table in tables for caveat in table.caveats})

        result.candidate_concepts = self._score_semantic_category("concepts", question, question_embedding, domain, top_k, result)
        result.candidate_metrics = self._score_semantic_category("metrics", question, question_embedding, domain, top_k, result)
        result.candidate_dimensions = self._score_semantic_category("dimensions", question, question_embedding, domain, top_k, result)
        return result

    def _embed_question(self, question: str) -> list[float] | None:
        if self.embedding_service is None:
            return None
        try:
            return self.embedding_service.embed(question)
        except Exception:
            return None

    def _embedding_similarity(self, question_embedding: list[float] | None, doc: str) -> float:
        if self.embedding_service is None or question_embedding is None:
            return 0.0
        try:
            return _cosine(question_embedding, self.embedding_service.embed(doc))
        except Exception:
            return 0.0

    def _score(
        self,
        *,
        question: str,
        question_embedding: list[float] | None,
        doc: str,
        certified: bool,
        popularity_source: Any,
        breakdown_key: str,
        result: RetrievalResult,
    ) -> float:
        embedding_similarity = self._embedding_similarity(question_embedding, doc)
        keyword_match = compute_keyword_match(question, doc)
        semantic_concept_match = keyword_match
        certification_boost = compute_certification_boost(certified)
        usage_popularity = _usage_popularity(popularity_source)
        final_score = (
            0.35 * embedding_similarity
            + 0.30 * keyword_match
            + 0.15 * semantic_concept_match
            + 0.10 * certification_boost
            + 0.10 * usage_popularity
        )
        result.score_breakdown[breakdown_key] = {
            "embedding": embedding_similarity,
            "keyword": keyword_match,
            "concept": semantic_concept_match,
            "cert": certification_boost,
            "popularity": usage_popularity,
            "final": final_score,
        }
        return final_score

    def _score_tables(
        self,
        question: str,
        question_embedding: list[float] | None,
        tables: list[TableMetadata],
        top_k: int,
        result: RetrievalResult,
    ) -> list[ScoredCandidate]:
        candidates = []
        for table in tables:
            doc = generate_table_doc(table)
            score = self._score(
                question=question,
                question_embedding=question_embedding,
                doc=doc,
                certified=table.certified,
                popularity_source=table,
                breakdown_key=f"table:{table.table_name}",
                result=result,
            )
            candidates.append(
                ScoredCandidate(
                    name=table.table_name,
                    score=score,
                    description=table.description,
                    domain=table.domain or "",
                )
            )
        return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)[:top_k]

    def _score_semantic_category(
        self,
        category: str,
        question: str,
        question_embedding: list[float] | None,
        domain: str | None,
        top_k: int,
        result: RetrievalResult,
    ) -> list[ScoredCandidate]:
        candidates = []
        items = domain_filter(list(self.semantic_registry_data.get(category, [])), domain)
        for item in items:
            name = str(_value(item, category[:-1], "name", "display_name", "term", default=""))
            if not name:
                continue
            description = str(_value(item, "description", "definition", default="") or "")
            item_domain = str(_value(item, "domain", default="") or "")
            doc = self._semantic_doc(category, item, name, description)
            status = str(_value(item, "status", default="") or "")
            score = self._score(
                question=question,
                question_embedding=question_embedding,
                doc=doc,
                certified=status == "certified",
                popularity_source=item,
                breakdown_key=f"{category[:-1]}:{name}",
                result=result,
            )
            candidates.append(ScoredCandidate(name=name, score=score, description=description, domain=item_domain))
        return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)[:top_k]

    def _semantic_doc(self, category: str, item: Any, name: str, description: str) -> str:
        fields = [category[:-1], name, description]
        for field in (
            "synonyms",
            "candidate_concepts",
            "concept",
            "allowed_dimensions",
            "physical_mappings",
            "default_concept_by_domain",
        ):
            value = _value(item, field, default=None)
            if value not in (None, "", [], {}):
                fields.append(str(value))
        return "\n".join(fields)

    def _candidate_columns(self, tables: list[TableMetadata], top_k: int) -> list[str]:
        columns: list[str] = []
        for table in tables:
            for column in table.columns:
                columns.append(f"{table.table_name}.{column.column_name}")
        return columns[: max(top_k * 5, top_k)]


__all__ = [
    "HybridRetriever",
    "RetrievalResult",
    "ScoredCandidate",
    "compute_certification_boost",
    "compute_keyword_match",
    "domain_filter",
]
