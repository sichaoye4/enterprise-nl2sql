from __future__ import annotations

from src.semantic_registry.resolver.common import ExtractedTerm, MatchType
from src.semantic_registry.resolver.term_index import TermIndex
from src.semantic_registry.yaml_schema.schemas import DimensionYaml, TermYaml


class TermExtractor:
    def __init__(self, terms: list[TermYaml], dimensions: list[DimensionYaml] | None = None) -> None:
        self.index = TermIndex(terms, dimensions=dimensions)

    def extract(self, question: str) -> list[ExtractedTerm]:
        return self.index.search_by_synonym(question)

    def extract_exact(self, question: str) -> list[ExtractedTerm]:
        return self.index.search(question)

    def extract_synonyms(self, question: str) -> list[ExtractedTerm]:
        return self.index.search_synonyms_only(question)


__all__ = ["ExtractedTerm", "MatchType", "TermExtractor"]
