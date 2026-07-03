from __future__ import annotations

from dataclasses import dataclass, field

from src.semantic_registry.resolver.common import ExtractedTerm, MatchType, normalize_tokens, tokenize_with_spans
from src.semantic_registry.yaml_schema.schemas import DimensionYaml, TermYaml


@dataclass(frozen=True)
class _PatternEntry:
    term: str
    match_type: MatchType
    pattern: str


@dataclass
class _TrieNode:
    children: dict[str, "_TrieNode"] = field(default_factory=dict)
    entries: list[_PatternEntry] = field(default_factory=list)


class TermIndex:
    def __init__(self, terms: list[TermYaml] | None = None, dimensions: list[DimensionYaml] | None = None) -> None:
        self._exact_root = _TrieNode()
        self._synonym_root = _TrieNode()
        self.terms: list[TermYaml] = []
        self.dimensions: list[DimensionYaml] = []
        if terms or dimensions:
            self.build_index(terms or [], dimensions=dimensions)

    def build_index(self, terms: list[TermYaml], dimensions: list[DimensionYaml] | None = None) -> None:
        self.terms = list(terms)
        self.dimensions = list(dimensions or [])
        self._exact_root = _TrieNode()
        self._synonym_root = _TrieNode()
        for term in terms:
            self._add_pattern(self._exact_root, term.term, term.term, MatchType.exact)
            if "_" in term.term:
                self._add_pattern(self._exact_root, term.term.replace("_", " "), term.term, MatchType.exact)
            for synonym in term.synonyms:
                self._add_pattern(self._synonym_root, synonym, term.term, MatchType.synonym)
        for dimension in self.dimensions:
            self._add_pattern(self._exact_root, dimension.dimension, dimension.dimension, MatchType.dimension)
            if "_" in dimension.dimension:
                self._add_pattern(
                    self._exact_root,
                    dimension.dimension.replace("_", " "),
                    dimension.dimension,
                    MatchType.dimension,
                )
            for synonym in dimension.synonyms:
                self._add_pattern(self._synonym_root, synonym, dimension.dimension, MatchType.dimension)

    def search(self, question: str) -> list[ExtractedTerm]:
        return self._resolve_overlaps(self._search_trie(self._exact_root, question))

    def search_by_synonym(self, question: str) -> list[ExtractedTerm]:
        matches = [
            *self._search_trie(self._exact_root, question),
            *self._search_trie(self._synonym_root, question),
        ]
        return self._resolve_overlaps(matches)

    def search_synonyms_only(self, question: str) -> list[ExtractedTerm]:
        return self._resolve_overlaps(self._search_trie(self._synonym_root, question))

    def _add_pattern(self, root: _TrieNode, pattern: str, term: str, match_type: MatchType) -> None:
        tokens = normalize_tokens(pattern)
        if not tokens:
            return
        node = root
        for token in tokens:
            node = node.children.setdefault(token, _TrieNode())
        entry = _PatternEntry(term=term, match_type=match_type, pattern=pattern)
        if entry not in node.entries:
            node.entries.append(entry)

    def _search_trie(self, root: _TrieNode, question: str) -> list[ExtractedTerm]:
        tokens = tokenize_with_spans(question)
        matches: list[ExtractedTerm] = []
        for start_index, start_token in enumerate(tokens):
            node = root
            for end_index in range(start_index, len(tokens)):
                token = tokens[end_index]
                next_node = node.children.get(token.normalized)
                if next_node is None:
                    break
                node = next_node
                for entry in node.entries:
                    matches.append(
                        ExtractedTerm(
                            term=entry.term,
                            text=question[start_token.start : token.end],
                            start_pos=start_token.start,
                            end_pos=token.end,
                            match_type=entry.match_type,
                        )
                    )
        return self._dedupe(matches)

    def _dedupe(self, matches: list[ExtractedTerm]) -> list[ExtractedTerm]:
        priority = {MatchType.exact: 2, MatchType.dimension: 1, MatchType.synonym: 0}
        unique: dict[tuple[str, int, int], ExtractedTerm] = {}
        for match in matches:
            key = (match.term, match.start_pos, match.end_pos)
            existing = unique.get(key)
            if existing is None or priority[match.match_type] > priority[existing.match_type]:
                unique[key] = match
        return list(unique.values())

    def _resolve_overlaps(self, matches: list[ExtractedTerm]) -> list[ExtractedTerm]:
        priority = {MatchType.exact: 2, MatchType.dimension: 1, MatchType.synonym: 0}
        selected: list[ExtractedTerm] = []
        for match in sorted(
            matches,
            key=lambda item: (
                -(item.end_pos - item.start_pos),
                -priority[item.match_type],
                item.start_pos,
                item.term,
            ),
        ):
            if any(self._conflicts(match, existing) for existing in selected):
                continue
            selected.append(match)
        return sorted(selected, key=lambda item: (item.start_pos, item.end_pos, item.term))

    def _conflicts(self, left: ExtractedTerm, right: ExtractedTerm) -> bool:
        same_span = left.start_pos == right.start_pos and left.end_pos == right.end_pos
        if same_span:
            return False
        return left.start_pos < right.end_pos and right.start_pos < left.end_pos
