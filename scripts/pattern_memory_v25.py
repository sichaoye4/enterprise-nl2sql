#!/usr/bin/env python3
"""Deterministic V2.5 pattern memory for NL2SQL few-shot retrieval."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.semantic_registry.registry.db_registry import DBRegistry, DatabaseProfile, maturity_tier_for_count


DEFAULT_PATTERN_DB = "~/.hermes/nl2sql_pattern_memory_v25.db"

STOP_WORDS = {
    "what",
    "which",
    "who",
    "whom",
    "whose",
    "where",
    "when",
    "why",
    "how",
    "many",
    "much",
    "show",
    "list",
    "find",
    "give",
    "return",
    "display",
    "the",
    "a",
    "an",
    "of",
    "in",
    "on",
    "for",
    "to",
    "from",
    "by",
    "with",
    "and",
    "or",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "that",
    "this",
    "these",
    "those",
    "each",
    "all",
    "per",
    "than",
}

AGGREGATE_WORDS = {
    "count",
    "number",
    "total",
    "sum",
    "average",
    "avg",
    "mean",
    "minimum",
    "maximum",
    "min",
    "max",
}


@dataclass(frozen=True)
class QuestionFeatures:
    query_type: str
    terms: list[str]
    expected_features: dict[str, Any]
    table_hints: list[str] = field(default_factory=list)
    column_hints: list[str] = field(default_factory=list)


@dataclass
class SQLPatternV25:
    question: str
    sql: str
    db_id: str
    query_type: str
    ast_features: dict[str, Any]
    tables_used: list[str]
    columns_used: list[str]
    question_terms: list[str]
    difficulty: str = ""
    match_count: int = 0
    created_at: float = 0.0
    pattern_id: int = 0


@dataclass
class PatternMatchV25:
    pattern: SQLPatternV25
    score: float
    match_reasons: list[str] = field(default_factory=list)


def question_terms(question: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_]*|\d+(?:\.\d+)?", (question or "").lower())
    return [w for w in words if len(w) > 1 and w not in STOP_WORDS]


def _jaccard(left: list[str] | set[str], right: list[str] | set[str]) -> float:
    lset = {x.lower() for x in left if x}
    rset = {x.lower() for x in right if x}
    union = lset | rset
    return len(lset & rset) / len(union) if union else 0.0


def _contains_any(text: str, pattern: str) -> bool:
    return bool(re.search(pattern, text or "", re.I))


def analyze_question(question: str, profile: DatabaseProfile | None = None) -> QuestionFeatures:
    text = question or ""
    lower = text.lower()
    terms = question_terms(text)

    wants_ranking = _contains_any(
        lower,
        r"\b(top|bottom|highest|lowest|largest|smallest|most|least|best|worst|first|last|rank|ranking)\b",
    )
    wants_ratio = _contains_any(
        lower,
        r"\b(rate|ratio|percent|percentage|proportion|share|fraction|per\s+cent)\b",
    )
    wants_aggregation = _contains_any(
        lower,
        r"\b(count|number of|how many|total|sum|average|avg|mean|min|max|minimum|maximum)\b",
    )
    wants_grouping = _contains_any(lower, r"\b(by|per|each|for each|grouped by|breakdown)\b")
    wants_comparison = _contains_any(lower, r"\b(compare|between|versus|vs|difference|more than|less than)\b")
    has_date_hint = _contains_any(lower, r"\b(year|month|day|date|after|before|during|in \d{4})\b")

    if wants_ratio:
        query_type = "ratio"
    elif wants_ranking:
        query_type = "top_n"
    elif wants_aggregation and wants_grouping:
        query_type = "agg_group_by"
    elif wants_aggregation:
        query_type = "simple_agg"
    elif wants_comparison:
        query_type = "agg_filter"
    else:
        query_type = "simple_select"

    expected = {
        "has_aggregation": wants_aggregation or wants_ratio,
        "has_group_by": wants_grouping,
        "has_order_by": wants_ranking,
        "has_limit": wants_ranking,
        "has_ratio": wants_ratio,
        "has_filter": bool(re.search(r"\b(where|with|without|after|before|between|over|under|more than|less than|equal)\b", lower)),
        "has_date_filter": has_date_hint,
        "has_comparison": wants_comparison,
    }

    table_hints: list[str] = []
    column_hints: list[str] = []
    if profile:
        token_set = set(terms)
        for table in profile.schema_snapshot.get("tables", []):
            table_name = table.get("name", "")
            table_tokens = re.findall(r"[a-zA-Z0-9]+", table_name.lower())
            if table_name.lower() in lower or token_set.intersection(table_tokens):
                table_hints.append(table_name.lower())
            for column in table.get("columns", []):
                column_name = column.get("name", "")
                column_tokens = re.findall(r"[a-zA-Z0-9]+", column_name.lower())
                if column_name.lower() in lower or token_set.intersection(column_tokens):
                    column_hints.append(f"{table_name.lower()}.{column_name.lower()}")

    return QuestionFeatures(
        query_type=query_type,
        terms=terms,
        expected_features=expected,
        table_hints=sorted(set(table_hints)),
        column_hints=sorted(set(column_hints)),
    )


def _query_type_expected_features(query_type: str) -> dict[str, bool]:
    """Translate a stored/V1-style query type into positive structural expectations."""
    if query_type == "subquery":
        return {"has_subquery": True}
    if query_type == "window_function":
        return {"has_window": True}
    if query_type == "join_agg":
        return {"has_join": True, "has_aggregation": True}
    if query_type == "join_simple":
        return {"has_join": True}
    if query_type == "ratio":
        return {"has_ratio": True}
    if query_type == "top_n":
        return {"has_order_by": True, "has_limit": True}
    if query_type == "agg_group_by":
        return {"has_aggregation": True, "has_group_by": True}
    if query_type == "agg_filter":
        return {"has_aggregation": True, "has_filter": True}
    if query_type == "simple_agg":
        return {"has_aggregation": True}
    return {}


def _unique_nonempty(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _fallback_sql_features(sql: str) -> tuple[dict[str, Any], list[str], list[str], str]:
    upper = (sql or "").upper()
    tables = [m.group(1).strip('"`[]').lower() for m in re.finditer(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w]*)", sql or "", re.I)]
    columns = [m.group(0).lower() for m in re.finditer(r"[A-Za-z_][\w]*\.[A-Za-z_][\w]*", sql or "")]
    features = {
        "join_count": len(re.findall(r"\bJOIN\b", upper)),
        "table_count": len(set(tables)),
        "aggregate_functions": sorted(set(fn.lower() for fn in re.findall(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", upper))),
        "aggregate_count": len(re.findall(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", upper)),
        "has_case": "CASE" in upper,
        "has_cast": "CAST" in upper,
        "has_group_by": "GROUP BY" in upper,
        "has_order_by": "ORDER BY" in upper,
        "has_limit": "LIMIT" in upper,
        "has_distinct": "DISTINCT" in upper,
        "has_subquery": upper.count("SELECT") > 1,
        "has_window": bool(re.search(r"\bOVER\s*\(", upper)),
        "has_having": "HAVING" in upper,
        "has_where": "WHERE" in upper,
        "has_ratio": "/" in (sql or ""),
    }
    return features, sorted(set(tables)), sorted(set(columns)), classify_query_type_from_features(features)


def extract_sql_features(sql: str) -> tuple[dict[str, Any], list[str], list[str], str]:
    """Extract deterministic AST features and schema footprint from SQL."""
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return _fallback_sql_features(sql)

    alias_to_table: dict[str, str] = {}
    tables: set[str] = set()
    for table in tree.find_all(exp.Table):
        table_name = table.name.lower()
        tables.add(table_name)
        alias = table.alias_or_name
        if alias:
            alias_to_table[alias.lower()] = table_name

    columns: set[str] = set()
    for column in tree.find_all(exp.Column):
        column_name = column.name.lower()
        table_name = (column.table or "").lower()
        resolved_table = alias_to_table.get(table_name, table_name)
        columns.add(f"{resolved_table}.{column_name}" if resolved_table else column_name)

    aggregate_functions = sorted({type(node).__name__.lower() for node in tree.find_all(exp.AggFunc)})
    select_count = sum(1 for _ in tree.find_all(exp.Select))
    features = {
        "join_count": sum(1 for _ in tree.find_all(exp.Join)),
        "table_count": len(tables),
        "aggregate_functions": aggregate_functions,
        "aggregate_count": sum(1 for _ in tree.find_all(exp.AggFunc)),
        "has_case": any(True for _ in tree.find_all(exp.Case)),
        "has_cast": any(True for _ in tree.find_all(exp.Cast)),
        "has_group_by": any(True for _ in tree.find_all(exp.Group)),
        "has_order_by": any(True for _ in tree.find_all(exp.Order)),
        "has_limit": any(True for _ in tree.find_all(exp.Limit)),
        "has_distinct": any(bool(sel.args.get("distinct")) for sel in tree.find_all(exp.Select)),
        "has_subquery": select_count > 1 or any(True for _ in tree.find_all(exp.Subquery)),
        "has_window": any(True for _ in tree.find_all(exp.Window)),
        "has_having": any(True for _ in tree.find_all(exp.Having)),
        "has_where": any(True for _ in tree.find_all(exp.Where)),
        "has_ratio": any(True for _ in tree.find_all(exp.Div)),
    }
    return features, sorted(tables), sorted(columns), classify_query_type_from_features(features)


def classify_query_type_from_features(features: dict[str, Any]) -> str:
    if features.get("has_subquery"):
        return "subquery"
    if features.get("join_count", 0) > 0 and features.get("aggregate_count", 0) > 0:
        return "join_agg"
    if features.get("join_count", 0) > 0:
        return "join_simple"
    if features.get("has_ratio"):
        return "ratio"
    if features.get("has_window"):
        return "window_function"
    if features.get("has_order_by") and features.get("has_limit"):
        return "top_n"
    if features.get("aggregate_count", 0) > 0 and features.get("has_group_by"):
        return "agg_group_by"
    if features.get("aggregate_count", 0) > 0 and features.get("has_where"):
        return "agg_filter"
    if features.get("aggregate_count", 0) > 0:
        return "simple_agg"
    return "simple_select"


class PatternStoreV25:
    def __init__(self, db_path: str = DEFAULT_PATTERN_DB):
        self.db_path = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS patterns_v25 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT NOT NULL,
                    sql TEXT NOT NULL,
                    db_id TEXT NOT NULL,
                    query_type TEXT NOT NULL,
                    ast_features TEXT NOT NULL,
                    tables_used TEXT NOT NULL,
                    columns_used TEXT NOT NULL,
                    question_terms TEXT NOT NULL,
                    difficulty TEXT NOT NULL DEFAULT '',
                    match_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    UNIQUE(question, sql, db_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_patterns_v25_db ON patterns_v25(db_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_patterns_v25_db_type ON patterns_v25(db_id, query_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_patterns_v25_type ON patterns_v25(query_type)")

    def add(self, pattern: SQLPatternV25) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO patterns_v25 (
                    question, sql, db_id, query_type, ast_features, tables_used,
                    columns_used, question_terms, difficulty, match_count, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pattern.question,
                    pattern.sql,
                    pattern.db_id,
                    pattern.query_type,
                    json.dumps(pattern.ast_features, sort_keys=True),
                    json.dumps(pattern.tables_used),
                    json.dumps(pattern.columns_used),
                    json.dumps(pattern.question_terms),
                    pattern.difficulty,
                    pattern.match_count,
                    pattern.created_at or time.time(),
                ),
            )
            return cur.rowcount > 0

    def increment_match(self, question: str, sql: str, db_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE patterns_v25
                SET match_count = match_count + 1
                WHERE question = ? AND sql = ? AND db_id = ?
                """,
                (question, sql, db_id),
            )

    def search(self, db_id: str = "", query_type: str = "", limit: int | None = 20) -> list[SQLPatternV25]:
        conditions: list[str] = []
        params: list[Any] = []
        if db_id:
            conditions.append("db_id = ?")
            params.append(db_id)
        if query_type:
            conditions.append("query_type = ?")
            params.append(query_type)
        where = " AND ".join(conditions) if conditions else "1=1"
        with self._connect() as conn:
            limit_sql = "LIMIT ?" if limit is not None else ""
            query_params = (*params, limit) if limit is not None else tuple(params)
            rows = conn.execute(
                f"""
                SELECT *
                FROM patterns_v25
                WHERE {where}
                ORDER BY match_count DESC, created_at DESC
                {limit_sql}
                """,
                query_params,
            ).fetchall()
        return [self._row_to_pattern(row) for row in rows]

    def count(self, db_id: str = "") -> int:
        with self._connect() as conn:
            if db_id:
                return int(conn.execute("SELECT COUNT(*) FROM patterns_v25 WHERE db_id = ?", (db_id,)).fetchone()[0])
            return int(conn.execute("SELECT COUNT(*) FROM patterns_v25").fetchone()[0])

    def counts_by_db(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT db_id, COUNT(*) AS count FROM patterns_v25 GROUP BY db_id").fetchall()
        return {row["db_id"]: int(row["count"]) for row in rows}

    def _row_to_pattern(self, row: sqlite3.Row) -> SQLPatternV25:
        return SQLPatternV25(
            question=row["question"],
            sql=row["sql"],
            db_id=row["db_id"],
            query_type=row["query_type"],
            ast_features=json.loads(row["ast_features"]),
            tables_used=json.loads(row["tables_used"]),
            columns_used=json.loads(row["columns_used"]),
            question_terms=json.loads(row["question_terms"]),
            difficulty=row["difficulty"],
            match_count=int(row["match_count"]),
            created_at=float(row["created_at"]),
            pattern_id=int(row["id"]),
        )


class PatternMemoryV25:
    """Tier-aware deterministic pattern memory."""

    def __init__(
        self,
        db_path: str = DEFAULT_PATTERN_DB,
        registry: DBRegistry | None = None,
    ):
        self.store = PatternStoreV25(db_path)
        self.registry = registry or DBRegistry()

    def ensure_database(self, db_id: str, db_path: str = "", db_root: str = "") -> DatabaseProfile | None:
        profile = self.registry.ensure_database(db_id=db_id, db_path=db_path, db_root=db_root, source="bird" if db_root else "")
        if profile:
            self.registry.set_pattern_count(db_id, self.store.count(db_id))
            return self.registry.get(db_id)
        return None

    def ingest(self, question: str, sql: str, db_id: str, difficulty: str = "") -> bool:
        ast_features, tables_used, columns_used, query_type = extract_sql_features(sql)
        pattern = SQLPatternV25(
            question=question,
            sql=sql,
            db_id=db_id,
            query_type=query_type,
            ast_features=ast_features,
            tables_used=tables_used,
            columns_used=columns_used,
            question_terms=question_terms(question),
            difficulty=difficulty,
            match_count=0,
            created_at=time.time(),
        )
        added = self.store.add(pattern)
        if added:
            self.registry.increment_pattern_count(db_id)
        return added

    def record_match(self, question: str, sql: str, db_id: str, db_path: str = "") -> None:
        if db_path:
            self.ensure_database(db_id, db_path=db_path)
        added = self.ingest(question, sql, db_id)
        self.store.increment_match(question, sql, db_id)
        if not added:
            self.registry.set_pattern_count(db_id, self.store.count(db_id))

    def retrieve(self, question: str, db_id: str, top_k: int = 3, query_type: str | None = None) -> list[PatternMatchV25]:
        profile = self.registry.get(db_id)
        self.registry.set_pattern_count(db_id, self.store.count(db_id))
        profile = self.registry.get(db_id) or profile
        qfeatures = analyze_question(question, profile)
        query_types = _unique_nonempty([query_type, qfeatures.query_type])
        if query_types:
            expected = dict(qfeatures.expected_features)
            for qtype in query_types:
                expected.update(_query_type_expected_features(qtype))
            qfeatures = QuestionFeatures(
                query_type=qfeatures.query_type,
                terms=qfeatures.terms,
                expected_features=expected,
                table_hints=qfeatures.table_hints,
                column_hints=qfeatures.column_hints,
            )
        pattern_count = self.store.count(db_id)
        tier = profile.maturity_tier if profile else maturity_tier_for_count(pattern_count)

        if tier == 5 or pattern_count <= 0:
            return []
        if tier == 1:
            candidates = self._same_db_candidate_union(db_id, query_types, same_db_limit=None)
            matches = [self._score_tier1(p, qfeatures, query_types) for p in candidates]
        elif tier == 2:
            candidates = self._same_db_candidate_union(db_id, query_types, same_db_limit=None)
            matches = [self._score_keyword_query_type(p, qfeatures, "tier2", query_types) for p in candidates]
        elif tier == 3:
            candidates = self._same_db_candidate_union(db_id, query_types, same_db_limit=None)
            matches = [
                PatternMatchV25(
                    p,
                    0.55 + (0.15 if p.query_type in query_types else 0.0) + min(p.match_count / 20, 0.3),
                    ["tier3:same_db", f"type:{1 if p.query_type in query_types else 0}"],
                )
                for p in candidates
            ]
        else:
            matches = self._retrieve_tier4(db_id, qfeatures, pattern_count, query_types)

        return self._dedupe(matches, top_k)

    def _same_db_candidate_union(
        self,
        db_id: str,
        query_types: list[str],
        same_db_limit: int | None,
        type_limit: int = 80,
    ) -> list[SQLPatternV25]:
        candidates: list[SQLPatternV25] = []
        seen: set[int] = set()

        def add(patterns: list[SQLPatternV25]) -> None:
            for pattern in patterns:
                key = pattern.pattern_id
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(pattern)

        for qtype in query_types:
            add(self.store.search(db_id=db_id, query_type=qtype, limit=type_limit))
        add(self.store.search(db_id=db_id, limit=same_db_limit))
        return candidates

    def _retrieve_tier4(
        self,
        db_id: str,
        qfeatures: QuestionFeatures,
        pattern_count: int,
        query_types: list[str],
    ) -> list[PatternMatchV25]:
        matches: list[PatternMatchV25] = []
        if pattern_count >= 3:
            for qtype in query_types:
                for pattern in self.store.search(db_id=db_id, query_type=qtype, limit=10):
                    matches.append(PatternMatchV25(pattern, 0.55 + min(pattern.match_count / 20, 0.2), ["tier4:own_query_type"]))
            if matches:
                return matches

        similar = self.registry.find_similar_databases(db_id, threshold=0.2, limit=5)
        for profile, db_score in similar:
            candidates = []
            for qtype in query_types:
                candidates.extend(self.store.search(db_id=profile.db_id, query_type=qtype, limit=8))
            if not candidates:
                candidates = self.store.search(db_id=profile.db_id, limit=5)
            for pattern in candidates:
                term_score = _jaccard(qfeatures.terms, pattern.question_terms)
                score = 0.35 * db_score + 0.35 * (1 if pattern.query_type in query_types else 0) + 0.3 * term_score
                matches.append(PatternMatchV25(pattern, score, [f"tier4:similar_db:{profile.db_id}:{db_score:.2f}"]))

        if not matches:
            for pattern in self.store.search(db_id=db_id, limit=5):
                matches.append(PatternMatchV25(pattern, 0.35, ["tier4:fallback_own"]))
        return matches

    def _score_tier1(
        self,
        pattern: SQLPatternV25,
        qfeatures: QuestionFeatures,
        query_types: list[str],
    ) -> PatternMatchV25:
        table_score = _jaccard(qfeatures.table_hints, pattern.tables_used)
        column_score = _jaccard(qfeatures.column_hints, pattern.columns_used)
        if not qfeatures.table_hints and not qfeatures.column_hints:
            footprint_score = _jaccard(qfeatures.terms, pattern.tables_used + pattern.columns_used)
        else:
            footprint_score = 0.65 * table_score + 0.35 * column_score

        feature_score, feature_hits = self._feature_similarity(pattern.ast_features, qfeatures.expected_features)
        type_score = 1.0 if pattern.query_type in query_types else 0.0
        term_score = _jaccard(qfeatures.terms, pattern.question_terms)
        popularity = min(pattern.match_count / 20, 1.0)
        score = 0.35 * footprint_score + 0.3 * feature_score + 0.2 * type_score + 0.1 * term_score + 0.05 * popularity
        reasons = [f"tier1:footprint:{footprint_score:.2f}", f"ast:{feature_score:.2f}", f"type:{type_score:.0f}"]
        if feature_hits:
            reasons.append("features:" + ",".join(feature_hits[:4]))
        return PatternMatchV25(pattern, score, reasons)

    def _score_keyword_query_type(
        self,
        pattern: SQLPatternV25,
        qfeatures: QuestionFeatures,
        prefix: str,
        query_types: list[str],
    ) -> PatternMatchV25:
        term_score = _jaccard(qfeatures.terms, pattern.question_terms)
        type_score = 1.0 if pattern.query_type in query_types else 0.0
        popularity = min(pattern.match_count / 20, 1.0)
        score = 0.55 * term_score + 0.25 * type_score + 0.1 + 0.1 * popularity
        return PatternMatchV25(pattern, score, [f"{prefix}:same_db", f"type:{type_score:.0f}", f"terms:{term_score:.2f}"])

    def _feature_similarity(
        self,
        ast_features: dict[str, Any],
        expected_features: dict[str, Any],
    ) -> tuple[float, list[str]]:
        actual = {
            "has_aggregation": bool(ast_features.get("aggregate_count", 0)),
            "has_group_by": bool(ast_features.get("has_group_by")),
            "has_order_by": bool(ast_features.get("has_order_by")),
            "has_limit": bool(ast_features.get("has_limit")),
            "has_ratio": bool(ast_features.get("has_ratio")),
            "has_filter": bool(ast_features.get("has_where") or ast_features.get("has_having")),
            "has_join": bool(ast_features.get("join_count", 0)),
            "has_subquery": bool(ast_features.get("has_subquery")),
            "has_window": bool(ast_features.get("has_window")),
            "has_distinct": bool(ast_features.get("has_distinct")),
            "has_having": bool(ast_features.get("has_having")),
        }
        weights = {
            "has_join": 1.5,
            "has_subquery": 1.5,
            "has_window": 1.4,
            "has_ratio": 1.3,
            "has_group_by": 1.1,
            "has_order_by": 1.0,
            "has_limit": 1.0,
            "has_aggregation": 1.0,
            "has_filter": 0.9,
            "has_distinct": 0.8,
            "has_having": 0.8,
        }

        expected_positive = [name for name in weights if expected_features.get(name)]
        if not expected_positive:
            score = 0.5
            if actual["has_subquery"] or actual["has_window"] or actual["has_ratio"]:
                score -= 0.15
            if actual["has_group_by"] or actual["has_order_by"] or actual["has_limit"]:
                score -= 0.10
            return max(0.0, score), []

        matched_weight = 0.0
        expected_weight = 0.0
        annotations: list[str] = []
        penalties = 0.0
        contradiction_penalties = {
            "has_join": 0.25,
            "has_subquery": 0.30,
            "has_window": 0.30,
            "has_ratio": 0.25,
            "has_order_by": 0.18,
            "has_limit": 0.18,
            "has_group_by": 0.16,
            "has_aggregation": 0.14,
            "has_filter": 0.12,
            "has_having": 0.12,
            "has_distinct": 0.08,
        }

        for name in expected_positive:
            weight = weights[name]
            expected_weight += weight
            if actual[name]:
                matched_weight += weight
                annotations.append(f"hit:{name}")
            else:
                penalties += contradiction_penalties.get(name, 0.0)
                annotations.append(f"miss:{name}")

        if not expected_features.get("has_order_by") and actual["has_order_by"]:
            penalties += 0.05
        if not expected_features.get("has_limit") and actual["has_limit"]:
            penalties += 0.05

        base = matched_weight / expected_weight if expected_weight else 0.5
        return max(0.0, min(1.0, base - penalties)), annotations

    def _dedupe(self, matches: list[PatternMatchV25], top_k: int) -> list[PatternMatchV25]:
        matches.sort(key=lambda m: (-m.score, -m.pattern.match_count, -m.pattern.created_at))
        seen_sql: set[str] = set()
        unique: list[PatternMatchV25] = []
        for match in matches:
            sql_key = re.sub(r"\s+", " ", match.pattern.sql.strip().lower())
            if sql_key in seen_sql:
                continue
            seen_sql.add(sql_key)
            unique.append(match)
            if len(unique) >= top_k:
                break
        return unique

    def build_prompt(
        self,
        question: str,
        db_id: str,
        schema_text: str,
        patterns: list[PatternMatchV25],
        evidence: str = "",
    ) -> str:
        parts = ["You are a SQLite expert. Generate a single SELECT statement."]
        if patterns:
            examples: list[str] = []
            for idx, match in enumerate(patterns[:3], 1):
                pattern = match.pattern
                examples.append(
                    "\n".join(
                        [
                            f"Example {idx}:",
                            f"Question: {pattern.question}",
                            f"Pattern: {pattern.query_type}; tables={', '.join(pattern.tables_used) or 'unknown'}",
                            f"SQL: {pattern.sql}",
                        ]
                    )
                )
            parts.append("Here are deterministic pattern-memory examples:")
            parts.append("\n\n".join(examples))
        parts.append("Database Schema:")
        parts.append(schema_text)
        if evidence:
            parts.append(f"Hint: {evidence}")
        parts.append(f"Question: {question}")
        parts.append("")
        parts.append("Return ONLY a JSON object:")
        parts.append('{"sql": "SELECT ...", "assumptions": [], "tables_used": [], "columns_used": [], "confidence": "high|medium|low", "reasoning_summary": "..."}')
        return "\n\n".join(parts)

    def seed_from_bird(self, dev_path: str, db_root: str = "", limit: int | None = None) -> dict[str, int]:
        with open(dev_path) as f:
            dev = json.load(f)
        if limit:
            dev = dev[:limit]

        counts = {"added": 0, "skipped": 0, "registered": 0}
        seen_dbs: set[str] = set()
        for idx, item in enumerate(dev, 1):
            db_id = item.get("db_id", "")
            if db_id and db_root and db_id not in seen_dbs:
                self.ensure_database(db_id, db_root=db_root)
                seen_dbs.add(db_id)
                counts["registered"] += 1
            if self.ingest(
                question=item.get("question", ""),
                sql=item.get("SQL", ""),
                db_id=db_id,
                difficulty=item.get("difficulty", ""),
            ):
                counts["added"] += 1
            else:
                counts["skipped"] += 1
            if idx % 500 == 0:
                print(f"  Seeded {idx}/{len(dev)}...")

        for db_id, count in self.store.counts_by_db().items():
            self.registry.set_pattern_count(db_id, count)
        return counts

    def stats(self) -> dict[str, Any]:
        with self.store._connect() as conn:
            by_db = dict(conn.execute("SELECT db_id, COUNT(*) FROM patterns_v25 GROUP BY db_id ORDER BY db_id").fetchall())
            by_type = dict(
                conn.execute("SELECT query_type, COUNT(*) FROM patterns_v25 GROUP BY query_type ORDER BY COUNT(*) DESC").fetchall()
            )
        return {"total": self.store.count(), "by_db": by_db, "by_type": by_type}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deterministic V2.5 pattern memory")
    parser.add_argument("--seed", action="store_true", help="Seed from BIRD dev.json")
    parser.add_argument("--stats", action="store_true", help="Show memory stats")
    parser.add_argument("--query", default="", help="Retrieve examples for a question")
    parser.add_argument("--db", default="", help="Database id for retrieval")
    args = parser.parse_args()

    base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    dev_path = os.path.join(base, "bird_bench", "dev", "dev_20240627", "dev.json")
    db_root = os.path.join(base, "bird_bench", "dev", "dev_20240627", "databases", "dev_databases")

    memory = PatternMemoryV25()
    if args.seed:
        print(memory.seed_from_bird(dev_path=dev_path, db_root=db_root))
    if args.stats:
        print(json.dumps(memory.stats(), indent=2))
    if args.query:
        db_path = os.path.join(db_root, args.db, f"{args.db}.sqlite") if args.db else ""
        if args.db and os.path.exists(db_path):
            memory.ensure_database(args.db, db_path=db_path)
        for match in memory.retrieve(args.query, args.db, top_k=3):
            print(f"{match.score:.3f} {match.pattern.db_id} {match.pattern.query_type}: {match.pattern.question}")
