from __future__ import annotations

import re
from enum import StrEnum
from typing import Iterator, NamedTuple

from pydantic import BaseModel


TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

_STEM_EXCEPTIONS = {
    "business",
    "days",
    "news",
    "sales",
    "series",
    "species",
    "status",
}


class MatchType(StrEnum):
    exact = "exact"
    synonym = "synonym"
    dimension = "dimension"


class ExtractedTerm(BaseModel):
    term: str
    text: str
    start_pos: int
    end_pos: int
    match_type: MatchType


class TokenSpan(NamedTuple):
    text: str
    normalized: str
    start: int
    end: int


def tokenize_with_spans(text: str) -> list[TokenSpan]:
    return [
        TokenSpan(match.group(0), _stem(match.group(0).lower()), match.start(), match.end())
        for match in TOKEN_RE.finditer(text)
    ]


def normalize_tokens(text: str) -> list[str]:
    return [_stem(match.group(0).lower()) for match in TOKEN_RE.finditer(text)]


def _stem(word: str) -> str:
    if len(word) <= 3 or word in _STEM_EXCEPTIONS:
        return word
    if word.endswith("ies") and len(word) > 4:
        return f"{word[:-3]}y"
    if word.endswith("ves") and len(word) > 4:
        return f"{word[:-3]}f"
    if word.endswith("es") and not word.endswith(("ss", "ies")):
        if word.endswith(("ches", "shes", "xes", "zes", "sses")):
            return word[:-2]
        return word[:-1]
    if word.endswith("s") and not word.endswith(("ss", "us")):
        return word[:-1]
    return word


def iter_ngrams(tokens: list[str], size: int) -> Iterator[tuple[int, list[str]]]:
    if size <= 0:
        return
    for index in range(0, max(len(tokens) - size + 1, 0)):
        yield index, tokens[index : index + size]
