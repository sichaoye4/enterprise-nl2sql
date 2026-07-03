from __future__ import annotations

from src.semantic_registry.resolver.common import iter_ngrams, normalize_tokens
from src.semantic_registry.yaml_schema.schemas import TermYaml


class SynonymMatcher:
    def match(self, question_tokens: list[str], terms: list[TermYaml]) -> list[tuple[str, list[TermYaml], float]]:
        tokens = [token.lower() for token in question_tokens]
        grouped: dict[str, tuple[str, list[TermYaml], float]] = {}
        for term in terms:
            for synonym in term.synonyms:
                synonym_tokens = normalize_tokens(synonym)
                if not synonym_tokens:
                    continue
                self._match_exact(tokens, synonym_tokens, term, grouped)
                self._match_abbreviation(tokens, synonym_tokens, term, grouped)
                self._match_partial(tokens, synonym_tokens, term, grouped)
        return sorted(grouped.values(), key=lambda item: (-item[2], -len(item[0]), item[0]))

    def _match_exact(
        self,
        tokens: list[str],
        synonym_tokens: list[str],
        term: TermYaml,
        grouped: dict[str, tuple[str, list[TermYaml], float]],
    ) -> None:
        for _, ngram in iter_ngrams(tokens, len(synonym_tokens)):
            if ngram == synonym_tokens:
                self._add(" ".join(ngram), term, 1.0, grouped)

    def _match_abbreviation(
        self,
        tokens: list[str],
        synonym_tokens: list[str],
        term: TermYaml,
        grouped: dict[str, tuple[str, list[TermYaml], float]],
    ) -> None:
        abbreviation = "".join(token[0] for token in synonym_tokens if token)
        if len(abbreviation) < 2:
            return
        for token in tokens:
            if token == abbreviation:
                self._add(token, term, 0.7, grouped)

    def _match_partial(
        self,
        tokens: list[str],
        synonym_tokens: list[str],
        term: TermYaml,
        grouped: dict[str, tuple[str, list[TermYaml], float]],
    ) -> None:
        if len(synonym_tokens) == 1:
            synonym = synonym_tokens[0]
            for token in tokens:
                if len(token) >= 3 and synonym.startswith(token) and token != synonym:
                    self._add(token, term, 0.7, grouped)
            return

        for size in range(len(synonym_tokens) - 1, 0, -1):
            prefix = synonym_tokens[:size]
            if sum(len(token) for token in prefix) < 4:
                continue
            for _, ngram in iter_ngrams(tokens, size):
                if ngram == prefix:
                    self._add(" ".join(ngram), term, 0.7, grouped)

    def _add(
        self,
        matched_text: str,
        term: TermYaml,
        confidence: float,
        grouped: dict[str, tuple[str, list[TermYaml], float]],
    ) -> None:
        key = matched_text.lower()
        if key not in grouped:
            grouped[key] = (matched_text, [term], confidence)
            return
        existing_text, existing_terms, existing_confidence = grouped[key]
        if term.term not in {item.term for item in existing_terms}:
            existing_terms.append(term)
        grouped[key] = (existing_text, existing_terms, max(existing_confidence, confidence))
