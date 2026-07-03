#!/usr/bin/env python3
"""
Pattern Memory System v2 — LLM-Powered Knowledge Base for NL2SQL.

Architecture:
  (Q, SQL) → LLMAnalyzer → EnrichedPattern → PatternStore
  New Q → LLMAnalyzer → SearchParams → Retriever → Top Patterns → FewShotBuilder → Prompt

Key Design:
  - LLM extracts business context + technical pattern + entities at ingestion time
  - LLM analyzes new queries to find semantically similar past patterns
  - Stores everything in SQLite for fast retrieval
"""

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Data Models ─────────────────────────────────────────────────────────────

QUERY_CATEGORIES = [
    "metric_query",       # "show me total revenue"
    "metric_by_dim",      # "revenue by region"
    "top_n_query",        # "top 10 products by sales"
    "list_query",         # "list all customers"
    "comparison_query",   # "compare revenue between Q1 and Q2"
    "ratio_query",        # "conversion rate"
    "ranking_query",      # "rank products by profit"
    "trend_query",        # "revenue over time"
    "filtered_list",      # "customers in California"
    "aggregate_join",     # "total orders per customer"
    "complex_query",      # multi-step or subquery
]

TECHNICAL_PATTERNS = [
    "simple_select",    # SELECT col FROM table WHERE ...
    "simple_agg",       # SELECT COUNT/SUM(col) FROM ...
    "agg_group_by",     # SELECT agg(col) GROUP BY dim
    "agg_filter",       # SELECT agg(col) WHERE ... GROUP BY
    "top_n_agg",        # SELECT agg(col) ... ORDER BY LIMIT N
    "top_n_simple",     # SELECT col ... ORDER BY LIMIT N (no agg)
    "join_simple",      # SELECT col FROM t1 JOIN t2
    "join_agg",         # SELECT agg FROM t1 JOIN t2 GROUP BY
    "ratio",            # SELECT col1 / col2 or agg(col1)/agg(col2)
    "subquery",         # IN (SELECT ...), EXISTS, correlated
    "multi_join_agg",   # 3+ tables joined with aggregation
    "window_function",  # ROW_NUMBER, RANK, etc.
]


@dataclass
class PatternMetadata:
    """LLM-extracted metadata for a query pattern."""
    business_intent: str
    business_category: str
    technical_pattern: str
    key_entities: list[str]
    metrics: list[str]
    dimensions: list[str]
    complexity: str  # simple / medium / complex
    tags: list[str]
    pattern_summary: str
    db_id: str
    confidence: float = 1.0


@dataclass
class EnrichedPattern:
    """A complete pattern entry with metadata + SQL."""
    question: str
    sql: str
    db_id: str
    metadata: PatternMetadata
    match_count: int = 0
    created_at: float = 0.0
    pattern_id: str = ""


@dataclass  
class SearchQuery:
    """LLM-extracted search parameters from a new question."""
    business_intent: str
    business_category: str
    expected_technical_pattern: str
    key_entities: list[str]
    metrics: list[str]
    dimensions: list[str]


@dataclass
class RankedPattern:
    """A pattern with relevance score."""
    pattern: EnrichedPattern
    score: float
    match_reasons: list[str] = field(default_factory=list)


# ── LLM Prompts ─────────────────────────────────────────────────────────────

PATTERN_ANALYSIS_PROMPT = """You are analyzing a natural language query and its SQL for a pattern library.

Question: {question}
SQL: {sql}
Database: {db_id}

Extract structured information. Return ONLY a JSON object:

{{
  "business_intent": "One sentence: what is the user trying to find out?",
  "business_category": "One of: {categories}",
  "technical_pattern": "One of: {technical_patterns}",
  "key_entities": ["entity1", "entity2"],
  "metrics": ["what is being measured"],
  "dimensions": ["grouping/filtering dimensions"],
  "complexity": "simple|medium|complex",
  "tags": ["3-5 descriptive tags mixing business and technical"],
  "pattern_summary": "A brief description of the query structure for pattern matching"
}}"""


QUERY_ANALYSIS_PROMPT = """Analyze this user question to find similar past queries:

Question: {question}
Database: {db_id}

Extract structured search parameters. Return ONLY a JSON object:

{{
  "business_intent": "What does the user want?",
  "business_category": "One of: {categories}",
  "expected_technical_pattern": "Expected SQL pattern type",
  "key_entities": ["business entities involved"],
  "metrics": ["what is being measured"],
  "dimensions": ["grouping/filtering dimensions"]
}}"""


RERANK_PROMPT = """Given a user question and candidate patterns, rank them by relevance.

Question: {question}

Candidate patterns:
{patterns}

For each pattern, provide a relevance score (0-10) and reason.
Return ONLY a JSON array:
[
  {{"pattern_idx": 0, "relevance_score": 8, "reason": "..."}},
  {{"pattern_idx": 1, "relevance_score": 3, "reason": "..."}}
]"""


# ── LLM Analyzer ────────────────────────────────────────────────────────────

class PatternAnalyzer:
    """LLM-powered pattern analysis and retrieval."""

    def __init__(self, model: str = "deepseek-v4-flash", reasoning_effort: str = "medium"):
        from src.semantic_registry.pipeline.llm_gateway import DeepSeekProvider
        self.provider = DeepSeekProvider(model=model, reasoning_effort=reasoning_effort)
    
    def _extract_json(self, raw: str) -> Optional[dict]:
        if not raw:
            return None
        try:
            start = raw.find("{")
            if start < 0:
                return None
            depth, in_str, quote = 0, False, ""
            for i in range(start, len(raw)):
                c = raw[i]
                if in_str:
                    if c == "\\":
                        pass
                    elif c == quote:
                        in_str = False
                elif c in ("'", '"'):
                    in_str = True
                    quote = c
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = raw[start:i+1]
                        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
                        return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass
        return None
    
    def analyze_pattern(self, question: str, sql: str, db_id: str) -> Optional[PatternMetadata]:
        """Analyze a (question, SQL) pair and extract metadata."""
        prompt = PATTERN_ANALYSIS_PROMPT.format(
            question=question,
            sql=sql,
            db_id=db_id,
            categories=", ".join(QUERY_CATEGORIES),
            technical_patterns=", ".join(TECHNICAL_PATTERNS),
        )
        try:
            raw = self.provider.generate(f"Return ONLY valid JSON.\n\n{prompt}")
            data = self._extract_json(raw)
            if not data:
                return None
            
            return PatternMetadata(
                business_intent=data.get("business_intent", ""),
                business_category=data.get("business_category", "metric_query"),
                technical_pattern=data.get("technical_pattern", "simple_select"),
                key_entities=[e.lower() for e in data.get("key_entities", [])],
                metrics=[m.lower() for m in data.get("metrics", [])],
                dimensions=[d.lower() for d in data.get("dimensions", [])],
                complexity=data.get("complexity", "simple"),
                tags=[t.lower() for t in data.get("tags", [])],
                pattern_summary=data.get("pattern_summary", ""),
                db_id=db_id,
            )
        except Exception as e:
            return None
    
    def analyze_question(self, question: str, db_id: str) -> Optional[SearchQuery]:
        """Analyze a question to extract search parameters."""
        prompt = QUERY_ANALYSIS_PROMPT.format(
            question=question,
            db_id=db_id,
            categories=", ".join(QUERY_CATEGORIES),
        )
        try:
            raw = self.provider.generate(f"Return ONLY valid JSON.\n\n{prompt}")
            data = self._extract_json(raw)
            if not data:
                return None
            
            return SearchQuery(
                business_intent=data.get("business_intent", ""),
                business_category=data.get("business_category", ""),
                expected_technical_pattern=data.get("expected_technical_pattern", ""),
                key_entities=[e.lower() for e in data.get("key_entities", [])],
                metrics=[m.lower() for m in data.get("metrics", [])],
                dimensions=[d.lower() for d in data.get("dimensions", [])],
            )
        except Exception as e:
            return None
    
    def rerank_patterns(self, question: str, candidates: list, top_k: int = 3) -> list:
        """Have the LLM rerank candidate patterns by relevance."""
        if not candidates:
            return []
        
        patterns_text = []
        for i, (p, score) in enumerate(candidates):
            meta = p.metadata
            patterns_text.append(
                f"[{i}] Intent: {meta.business_intent}\n"
                f"    Entities: {', '.join(meta.key_entities)}\n"
                f"    Technical: {meta.technical_pattern}\n"
                f"    Tags: {', '.join(meta.tags)}\n"
                f"    Question: {p.question[:80]}\n"
            )
        
        prompt = RERANK_PROMPT.format(
            question=question,
            patterns="\n".join(patterns_text),
        )
        
        try:
            raw = self.provider.generate(f"Return ONLY valid JSON.\n\n{prompt}")
            data = self._extract_json(raw)
            if not data or not isinstance(data, list):
                # Fallback: use original order
                return [c[0] for c in candidates[:top_k]]
            
            # Sort by LLM score
            scores = {}
            for entry in data:
                idx = entry.get("pattern_idx")
                score = entry.get("relevance_score", 0)
                scores[idx] = score
            
            ranked = sorted(
                [(candidates[i][0], scores.get(i, 0)) for i in range(len(candidates)) if i < len(candidates)],
                key=lambda x: -x[1]
            )
            return [p for p, s in ranked[:top_k]]
        
        except Exception:
            return [c[0] for c in candidates[:top_k]]


# ── Pattern Store ───────────────────────────────────────────────────────────

class PatternStore:
    """SQLite-backed persistent store for enriched patterns."""

    def __init__(self, db_path: str = "~/.hermes/nl2sql_pattern_memory.db"):
        self.db_path = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS enriched_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                sql TEXT NOT NULL,
                db_id TEXT NOT NULL,
                metadata TEXT NOT NULL,
                business_intent TEXT DEFAULT '',
                business_category TEXT DEFAULT '',
                technical_pattern TEXT DEFAULT '',
                entities TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                match_count INTEGER DEFAULT 0,
                created_at REAL DEFAULT (strftime('%s','now')),
                UNIQUE(question, sql, db_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ep_bizcat ON enriched_patterns(business_category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ep_techpat ON enriched_patterns(technical_pattern)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ep_db ON enriched_patterns(db_id)")
        conn.commit()
        conn.close()

    def add(self, pattern: EnrichedPattern) -> bool:
        conn = sqlite3.connect(self.db_path)
        try:
            meta_json = json.dumps({
                "business_intent": pattern.metadata.business_intent,
                "business_category": pattern.metadata.business_category,
                "technical_pattern": pattern.metadata.technical_pattern,
                "key_entities": pattern.metadata.key_entities,
                "metrics": pattern.metadata.metrics,
                "dimensions": pattern.metadata.dimensions,
                "complexity": pattern.metadata.complexity,
                "tags": pattern.metadata.tags,
                "pattern_summary": pattern.metadata.pattern_summary,
                "db_id": pattern.metadata.db_id,
            })
            conn.execute(
                """INSERT OR IGNORE INTO enriched_patterns 
                   (question, sql, db_id, metadata, business_intent, business_category, 
                    technical_pattern, entities, tags, match_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pattern.question, pattern.sql, pattern.db_id, meta_json,
                    pattern.metadata.business_intent,
                    pattern.metadata.business_category,
                    pattern.metadata.technical_pattern,
                    ",".join(pattern.metadata.key_entities),
                    ",".join(pattern.metadata.tags),
                    pattern.match_count,
                    pattern.created_at or time.time(),
                )
            )
            conn.commit()
            return True
        except Exception as e:
            return False
        finally:
            conn.close()

    def increment_match(self, question: str, sql: str, db_id: str):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE enriched_patterns SET match_count = match_count + 1 WHERE question = ? AND sql = ? AND db_id = ?",
            (question, sql, db_id)
        )
        conn.commit()
        conn.close()

    def search_by_entities(self, entities: list[str], limit: int = 20) -> list:
        """Find patterns mentioning any of the given entities."""
        if not entities:
            return []
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        
        conditions = []
        for e in entities:
            conditions.append("entities LIKE ?")
        where = " OR ".join(conditions)
        params = [f"%{e}%" for e in entities]
        
        rows = conn.execute(
            f"SELECT * FROM enriched_patterns WHERE {where} ORDER BY match_count DESC, created_at DESC LIMIT ?",
            (*params, limit)
        ).fetchall()
        conn.close()
        return [self._row_to_pattern(r) for r in rows]

    def search_by_technical_pattern(self, pattern: str, limit: int = 20) -> list:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM enriched_patterns WHERE technical_pattern = ? ORDER BY match_count DESC LIMIT ?",
            (pattern, limit)
        ).fetchall()
        conn.close()
        return [self._row_to_pattern(r) for r in rows]

    def search_by_category(self, category: str, limit: int = 20) -> list:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM enriched_patterns WHERE business_category = ? ORDER BY match_count DESC LIMIT ?",
            (category, limit)
        ).fetchall()
        conn.close()
        return [self._row_to_pattern(r) for r in rows]

    def search_same_db(self, db_id: str, limit: int = 10) -> list:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM enriched_patterns WHERE db_id = ? ORDER BY match_count DESC, created_at DESC LIMIT ?",
            (db_id, limit)
        ).fetchall()
        conn.close()
        return [self._row_to_pattern(r) for r in rows]

    def _row_to_pattern(self, r) -> EnrichedPattern:
        meta_data = json.loads(r["metadata"]) if isinstance(r["metadata"], str) else r["metadata"]
        meta = PatternMetadata(
            business_intent=meta_data.get("business_intent", ""),
            business_category=meta_data.get("business_category", ""),
            technical_pattern=meta_data.get("technical_pattern", ""),
            key_entities=meta_data.get("key_entities", []),
            metrics=meta_data.get("metrics", []),
            dimensions=meta_data.get("dimensions", []),
            complexity=meta_data.get("complexity", "simple"),
            tags=meta_data.get("tags", []),
            pattern_summary=meta_data.get("pattern_summary", ""),
            db_id=r["db_id"],
        )
        return EnrichedPattern(
            question=r["question"],
            sql=r["sql"],
            db_id=r["db_id"],
            metadata=meta,
            match_count=r["match_count"],
            created_at=r["created_at"],
            pattern_id=str(r["id"]),
        )

    def count(self) -> int:
        conn = sqlite3.connect(self.db_path)
        c = conn.execute("SELECT COUNT(*) FROM enriched_patterns").fetchone()[0]
        conn.close()
        return c


# ── Pattern Memory (Main Facade) ───────────────────────────────────────────

class PatternMemory:
    """Main interface: LLM-powered pattern memory for few-shot NL2SQL."""

    def __init__(self, model: str = "deepseek-v4-flash", reasoning_effort: str = "medium"):
        self._load_env()  # Must be first: loads API key before creating provider
        self.store = PatternStore()
        self.analyzer = PatternAnalyzer(model=model, reasoning_effort=reasoning_effort)

    def _load_env(self):
        env_path = os.path.expanduser("~/.hermes/.env")
        if os.path.exists(env_path):
            for line in open(env_path):
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k == "DEEPSEEK_API_KEY" and not os.environ.get("DEEPSEEK_API_KEY"):
                        os.environ[k] = v

    def ingest(self, question: str, sql: str, db_id: str) -> Optional[EnrichedPattern]:
        """Analyze and store a single (Q, SQL) pair."""
        metadata = self.analyzer.analyze_pattern(question, sql, db_id)
        if not metadata:
            return None
        
        pattern = EnrichedPattern(
            question=question, sql=sql, db_id=db_id,
            metadata=metadata, match_count=0, created_at=time.time(),
        )
        self.store.add(pattern)
        return pattern

    def ingest_batch(self, questions: list) -> dict:
        """Ingest multiple (Q, SQL, DB) triples. Returns counts."""
        counts = {"ingested": 0, "failed": 0, "skipped": 0}
        for i, (q, s, db) in enumerate(questions):
            if self.store.count() > 0:
                # Quick check if already exists
                pass
            result = self.ingest(q, s, db)
            if result:
                counts["ingested"] += 1
            else:
                counts["failed"] += 1
            if (i + 1) % 50 == 0:
                print(f"  Ingested {i+1}/{len(questions)}...")
        return counts

    def retrieve(self, question: str, db_id: str = None, top_k: int = 3) -> list[EnrichedPattern]:
        """Find the best few-shot patterns for a question using LLM analysis + reranking."""
        # Step 1: LLM analyzes the question
        search = self.analyzer.analyze_question(question, db_id or "")
        if not search:
            return []
        
        # Step 2: Multi-strategy retrieval
        candidates = []  # (pattern, score)
        seen = set()
        
        # Strategy A: Match by entities
        if search.key_entities:
            entity_matches = self.store.search_by_entities(search.key_entities, limit=10)
            for p in entity_matches:
                if p.pattern_id not in seen:
                    seen.add(p.pattern_id)
                    entity_overlap = len(set(search.key_entities) & set(p.metadata.key_entities))
                    total_entities = len(set(search.key_entities) | set(p.metadata.key_entities))
                    score = entity_overlap / total_entities if total_entities > 0 else 0
                    candidates.append((p, score * 0.7 + 0.3 * min(p.match_count / 10, 1)))
        
        # Strategy B: Match by technical pattern
        if search.expected_technical_pattern:
            tech_matches = self.store.search_by_technical_pattern(search.expected_technical_pattern, limit=10)
            for p in tech_matches:
                if p.pattern_id not in seen:
                    seen.add(p.pattern_id)
                    candidates.append((p, 0.5 + 0.3 * min(p.match_count / 10, 1)))
        
        # Strategy C: Same DB + business category
        if db_id:
            db_matches = self.store.search_same_db(db_id, limit=5)
            for p in db_matches:
                if p.pattern_id not in seen:
                    seen.add(p.pattern_id)
                    # Same DB bonus
                    bonus = 0.3 if p.metadata.business_category == search.business_category else 0.1
                    candidates.append((p, bonus + 0.2 * min(p.match_count / 10, 1)))
        
        if not candidates:
            return []
        
        # Step 3: LLM reranks top candidates
        candidates.sort(key=lambda x: -x[1])
        top_candidates = candidates[:10]
        
        reranked = self.analyzer.rerank_patterns(question, top_candidates, top_k)
        return reranked[:top_k]

    def build_few_shot_prompt(self, question: str, schema_text: str, 
                              patterns: list[EnrichedPattern], evidence: str = None) -> str:
        """Build a prompt with few-shot examples from retrieved patterns."""
        parts = ["You are a SQLite expert. Generate a single SELECT statement."]
        
        if patterns:
            parts.append("Here are examples of similar queries:")
            examples = []
            for i, p in enumerate(patterns):
                meta = p.metadata
                ex = (
                    f"Example {i+1}:\n"
                    f"Business context: {meta.business_intent}\n"
                    f"Question: {p.question}\n"
                    f"SQL: {p.sql}\n"
                )
                examples.append(ex)
            parts.append("\n".join(examples))
        
        parts.append("Database Schema:\n" + schema_text)
        if evidence:
            parts.append(f"Hint: {evidence}")
        parts.append(f"Question: {question}")
        parts.append('Return ONLY: {"sql": "SELECT ...", "assumptions": [], "tables_used": [], "columns_used": [], "confidence": "high|medium|low", "reasoning_summary": "..."}')
        
        return "\n\n".join(parts)

    def record_success(self, question: str, sql: str, db_id: str):
        """Increment match count for a successful pattern."""
        self.store.increment_match(question, sql, db_id)

    def seed_from_bird(self, dev_path: str = None, limit: int = None) -> dict:
        """Seed the pattern memory from BIRD-SQL dev set."""
        if dev_path is None:
            dev_path = os.path.join(
                os.path.dirname(__file__), "..",
                "bird_bench/dev/dev_20240627/dev.json"
            )
        with open(dev_path) as f:
            dev = json.load(f)
        
        if limit:
            dev = dev[:limit]
        
        print(f"Seeding pattern memory from {len(dev)} BIRD questions...")
        questions = [(q["question"], q["SQL"], q.get("db_id", "")) for q in dev]
        
        total = len(questions)
        # Use existing batch ingestion
        counts = {"ingested": 0, "failed": 0, "skipped": 0}
        for i, (q, s, db) in enumerate(questions):
            result = self.ingest(q, s, db)
            if result:
                counts["ingested"] += 1
            else:
                counts["failed"] += 1
            if (i + 1) % 100 == 0:
                print(f"  Progress: {i+1}/{total} ({counts['ingested']} ingested, {counts['failed']} failed)")
        
        print(f"  Done: {counts['ingested']} ingested, {counts['failed']} failed")
        print(f"  Total in store: {self.store.count()}")
        return counts

    def stats(self) -> dict:
        """Get memory statistics."""
        conn = sqlite3.connect(self.store.db_path)
        by_category = dict(conn.execute(
            "SELECT business_category, COUNT(*) FROM enriched_patterns GROUP BY business_category ORDER BY COUNT(*) DESC"
        ).fetchall())
        by_technical = dict(conn.execute(
            "SELECT technical_pattern, COUNT(*) FROM enriched_patterns GROUP BY technical_pattern ORDER BY COUNT(*) DESC"
        ).fetchall())
        by_db = dict(conn.execute(
            "SELECT db_id, COUNT(*) FROM enriched_patterns GROUP BY db_id ORDER BY db_id"
        ).fetchall())
        conn.close()
        return {
            "total": self.store.count(),
            "by_category": by_category,
            "by_technical": by_technical,
            "by_db": by_db,
        }


# ── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="LLM-Powered Pattern Memory")
    parser.add_argument("--seed", type=int, default=None, help="Seed from BIRD (optional limit)")
    parser.add_argument("--stats", action="store_true", help="Show statistics")
    parser.add_argument("--query", type=str, default=None, help="Test retrieval for a query")
    parser.add_argument("--db", type=str, default="", help="Database for test query")
    parser.add_argument("--model", type=str, default="deepseek-v4-flash", help="LLM model")
    parser.add_argument("--reasoning", type=str, default="medium", help="Reasoning effort")
    args = parser.parse_args()
    
    memory = PatternMemory(model=args.model, reasoning_effort=args.reasoning)
    
    if args.seed is not None:
        memory.seed_from_bird(limit=args.seed if args.seed > 0 else None)
    
    if args.stats:
        s = memory.stats()
        print(f"\nTotal patterns: {s['total']}")
        print(f"\nBy business category:")
        for k, v in sorted(s["by_category"].items(), key=lambda x: -x[1]):
            print(f"  {k:25} {v}")
        print(f"\nBy technical pattern:")
        for k, v in sorted(s["by_technical"].items(), key=lambda x: -x[1]):
            print(f"  {k:25} {v}")
    
    if args.query:
        print(f"\nQuery: {args.query}")
        print(f"Retrieving patterns...")
        patterns = memory.retrieve(args.query, db_id=args.db, top_k=3)
        print(f"Found {len(patterns)} patterns:")
        for i, p in enumerate(patterns):
            print(f"\n  [{i+1}] (matches: {p.match_count})")
            print(f"  Intent: {p.metadata.business_intent[:60]}")
            print(f"  Entities: {', '.join(p.metadata.key_entities)}")
            print(f"  Question: {p.question[:60]}")
            print(f"  SQL: {p.sql[:80]}")
