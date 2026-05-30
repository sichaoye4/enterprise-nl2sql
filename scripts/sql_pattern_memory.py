#!/usr/bin/env python3
"""
SQL Pattern Memory System — Few-shot prompt builder from successful patterns.

Architecture:
  PatternCapture  →  PatternStore (SQLite)  →  PatternRetriever  →  FewShotPromptBuilder

Query Types:
  simple_select, simple_agg, agg_group_by, agg_filter, top_n,
  ratio, join_simple, join_agg, subquery, complex

Usage:
  from sql_pattern_memory import SQLPatternMemory
  
  memory = SQLPatternMemory("bird_bench/pattern_memory.db")
  memory.seed_from_bird()  # Initialize with BIRD gold SQL
  
  # For a new query:
  patterns = memory.retrieve(question, db_id, query_type)
  few_shot_prompt = memory.build_few_shot(patterns)
"""

import json
import os
import re
import sqlite3
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict


# ── Query Type Classification ───────────────────────────────────────────────

QUERY_TYPES = [
    "simple_select",  # SELECT col FROM table WHERE ...
    "simple_agg",     # SELECT COUNT/SUM(col) FROM ...
    "agg_group_by",   # SELECT agg(col) GROUP BY dim ORDER BY ...
    "agg_filter",     # SELECT agg(col) FROM ... WHERE ... GROUP BY ...
    "top_n",          # ... ORDER BY ... LIMIT N
    "ratio",          # SELECT col1 / col2 or MAX(col1/col2)
    "join_simple",    # SELECT col FROM t1 JOIN t2 ON ...
    "join_agg",       # SELECT agg(col) FROM t1 JOIN t2 ON ... GROUP BY
    "subquery",       # WHERE col IN (SELECT ...) or EXISTS
    "complex",        # Multiple of the above
]


def classify_query_type(sql: str) -> str:
    """Classify a SQL query into a type for pattern matching."""
    upper = sql.upper().strip()
    
    features = {
        "has_join": bool(re.search(r'\bJOIN\b', upper)),
        "has_subquery": upper.count("SELECT") > 1,
        "has_group_by": "GROUP BY" in upper,
        "has_order_by": "ORDER BY" in upper,
        "has_where": "WHERE" in upper,
        "has_limit": "LIMIT" in upper,
        "has_agg": bool(re.search(r'\b(COUNT|SUM|AVG|MIN|MAX)\s*\(', upper)),
        "has_ratio": "/" in sql and bool(re.search(r'\bCOUNT|SUM|AVG|MIN|MAX|CAST\b', upper)),
        "has_distinct": "DISTINCT" in upper,
    }
    
    if features["has_subquery"]:
        return "subquery"
    if features["has_join"] and features["has_agg"]:
        return "join_agg"
    if features["has_join"]:
        return "join_simple"
    if features["has_ratio"]:
        return "ratio"
    if features["has_agg"] and features["has_group_by"] and features["has_limit"]:
        return "top_n"
    if features["has_agg"] and features["has_group_by"]:
        return "agg_group_by"
    if features["has_agg"] and features["has_where"]:
        return "agg_filter"
    if features["has_agg"]:
        return "simple_agg"
    if features["has_limit"] and features["has_order_by"]:
        return "top_n"
    
    return "simple_select"


def extract_terms_from_question(question: str) -> list[str]:
    """Extract key terms from a question for similarity matching."""
    # Remove stop words, keep nouns and numbers
    stop_words = {"what", "is", "the", "of", "in", "for", "to", "a", "an",
                  "and", "or", "are", "were", "was", "be", "been", "with",
                  "that", "this", "these", "those", "from", "by", "at",
                  "on", "all", "each", "every", "please", "list", "show",
                  "give", "me", "name", "find", "how", "many", "much",
                  "do", "does", "did", "has", "have", "had", "which",
                  "whose", "whom", "who", "where", "when", "why", "than"}
    
    # Extract meaningful words
    words = re.findall(r"[a-zA-Z]+", question.lower())
    return [w for w in words if w not in stop_words and len(w) > 2]


def schema_fingerprint(db_id: str, tables: list[str]) -> str:
    """Generate a stable fingerprint for a schema."""
    raw = f"{db_id}:{','.join(sorted(tables))}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


# ── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass
class SQLPattern:
    question: str
    sql: str
    db_id: str
    query_type: str
    schema_hash: str
    terms: list[str] = field(default_factory=list)
    difficulty: str = ""
    match_count: int = 0
    created_at: float = 0.0


@dataclass
class PatternMatch:
    pattern: SQLPattern
    score: float
    match_reasons: list[str] = field(default_factory=list)


# ── Pattern Store ───────────────────────────────────────────────────────────

class SQLPatternStore:
    """SQLite-backed persistent store for SQL patterns."""
    
    def __init__(self, db_path: str = "~/.hermes/sql_patterns.db"):
        self.db_path = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                sql TEXT NOT NULL,
                db_id TEXT NOT NULL,
                query_type TEXT NOT NULL,
                schema_hash TEXT NOT NULL,
                terms TEXT DEFAULT '',
                difficulty TEXT DEFAULT '',
                match_count INTEGER DEFAULT 0,
                created_at REAL DEFAULT (strftime('%s','now')),
                UNIQUE(question, sql, db_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_patterns_db 
            ON patterns(db_id, query_type)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_patterns_type
            ON patterns(query_type)
        """)
        conn.commit()
        conn.close()
    
    def add(self, pattern: SQLPattern) -> bool:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO patterns 
                   (question, sql, db_id, query_type, schema_hash, terms, difficulty, match_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pattern.question, pattern.sql, pattern.db_id, pattern.query_type,
                 pattern.schema_hash, ",".join(pattern.terms), pattern.difficulty,
                 pattern.match_count, pattern.created_at or time.time())
            )
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()
    
    def increment_match(self, question: str, sql: str, db_id: str):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE patterns SET match_count = match_count + 1 WHERE question = ? AND sql = ? AND db_id = ?",
            (question, sql, db_id)
        )
        conn.commit()
        conn.close()
    
    def search(self, db_id: str = None, query_type: str = None, 
               schema_hash: str = None, limit: int = 10) -> list[SQLPattern]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        
        conditions = []
        params = []
        if db_id:
            conditions.append("db_id = ?"); params.append(db_id)
        if query_type:
            conditions.append("query_type = ?"); params.append(query_type)
        if schema_hash:
            conditions.append("schema_hash = ?"); params.append(schema_hash)
        
        where = " AND ".join(conditions) if conditions else "1=1"
        rows = conn.execute(
            f"SELECT * FROM patterns WHERE {where} ORDER BY match_count DESC, created_at DESC LIMIT ?",
            (*params, limit)
        ).fetchall()
        conn.close()
        
        return [
            SQLPattern(
                question=r["question"], sql=r["sql"], db_id=r["db_id"],
                query_type=r["query_type"], schema_hash=r["schema_hash"],
                terms=r["terms"].split(",") if r["terms"] else [],
                difficulty=r["difficulty"], match_count=r["match_count"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
    
    def count(self, db_id: str = None) -> int:
        conn = sqlite3.connect(self.db_path)
        if db_id:
            count = conn.execute("SELECT COUNT(*) FROM patterns WHERE db_id = ?", (db_id,)).fetchone()[0]
        else:
            count = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
        conn.close()
        return count


# ── Pattern Retriever ───────────────────────────────────────────────────────

class PatternRetriever:
    """Retrieve relevant patterns for a query using multi-strategy matching."""
    
    def __init__(self, store: SQLPatternStore):
        self.store = store
    
    def retrieve(self, question: str, db_id: str, query_type: str,
                 schema_hash: str = None, top_k: int = 3) -> list[PatternMatch]:
        """Find the best matching patterns using tiered strategy."""
        question_terms = set(extract_terms_from_question(question))
        matches = []
        
        # Strategy 1: Same DB + same query type + term overlap
        candidates = self.store.search(db_id=db_id, query_type=query_type, limit=20)
        for p in candidates:
            p_terms = set(p.terms)
            overlap = len(question_terms & p_terms)
            total = len(question_terms | p_terms)
            score = overlap / total if total > 0 else 0
            # Bonus for exact term match priority and match_count
            score = score * 0.6 + min(p.match_count / 10, 0.4)
            if score > 0:
                matches.append(PatternMatch(p, score, [f"same_db_type_term:{score:.2f}"]))
        
        # Strategy 2: Same query type + term overlap (cross-DB)
        if len(matches) < top_k:
            candidates = self.store.search(query_type=query_type, limit=20)
            seen = {m.pattern.question for m in matches}
            for p in candidates:
                if p.question in seen:
                    continue
                p_terms = set(p.terms)
                overlap = len(question_terms & p_terms)
                total = len(question_terms | p_terms)
                score = (overlap / total) * 0.5 if total > 0 else 0
                if score > 0:
                    matches.append(PatternMatch(p, score, [f"same_type_term:{score:.2f}"]))
                    seen.add(p.question)
        
        # Strategy 3: Same DB, similar query type, high match_count
        if len(matches) < top_k:
            candidates = self.store.search(db_id=db_id, limit=10)
            seen = {m.pattern.question for m in matches}
            for p in candidates:
                if p.question in seen:
                    continue
                score = min(p.match_count / 20, 0.5)
                if score > 0.1:
                    matches.append(PatternMatch(p, score, [f"popular_same_db:{score:.2f}"]))
                    seen.add(p.question)
        
        # Sort and deduplicate
        matches.sort(key=lambda m: -m.score)
        seen_sql = set()
        unique = []
        for m in matches:
            if m.pattern.sql not in seen_sql:
                seen_sql.add(m.pattern.sql)
                unique.append(m)
        
        return unique[:top_k]
    
    def seed_from_data(self, dev_data: list[dict], tables_data: list[dict] = None):
        """Seed the pattern store from BIRD's gold SQL + questions."""
        counts = {"added": 0, "skipped": 0}
        
        # Build schema hashes
        schema_hashes = {}
        if tables_data:
            for t in tables_data:
                table_names = t["table_names_original"]
                schema_hashes[t["db_id"]] = schema_fingerprint(t["db_id"], table_names)
        
        for i, q in enumerate(dev_data):
            sql = q.get("SQL", "")
            question = q.get("question", "")
            db_id = q.get("db_id", "")
            difficulty = q.get("difficulty", "")
            
            if not sql or not question:
                counts["skipped"] += 1
                continue
            
            qtype = classify_query_type(sql)
            terms = extract_terms_from_question(question)
            shash = schema_hashes.get(db_id, schema_fingerprint(db_id, []))
            
            pattern = SQLPattern(
                question=question, sql=sql, db_id=db_id,
                query_type=qtype, schema_hash=shash,
                terms=terms, difficulty=difficulty,
                match_count=0, created_at=time.time(),
            )
            
            if self.store.add(pattern):
                counts["added"] += 1
            else:
                counts["skipped"] += 1
            
            if (i + 1) % 500 == 0:
                print(f"  Seeded {i+1}/{len(dev_data)}...")
        
        return counts


# ── Few Shot Prompt Builder ─────────────────────────────────────────────────

class FewShotPromptBuilder:
    """Build few-shot prompts from retrieved patterns."""
    
    def build(self, question: str, db_id: str, schema_text: str,
              patterns: list[PatternMatch], evidence: str = None) -> str:
        """Build a complete prompt with few-shot examples + schema + question."""
        parts = ["You are a SQLite expert. Generate a single SELECT statement."]
        
        # Few-shot examples
        if patterns:
            examples = []
            for i, m in enumerate(patterns[:3]):
                p = m.pattern
                examples.append(f"Example {i+1}:")
                examples.append(f"Question: {p.question}")
                if p.difficulty:
                    examples.append(f"Difficulty: {p.difficulty}")
                examples.append(f"SQL: {p.sql}")
                examples.append("")
            parts.append("Here are examples of similar questions and their SQL:")
            parts.append("\n".join(examples))
        
        # Schema
        parts.append("Database Schema:")
        parts.append(schema_text)
        
        # Evidence/domain hint
        if evidence:
            parts.append(f"Hint: {evidence}")
        
        # Question
        parts.append(f"Question: {question}")
        
        # Output format
        parts.append("")
        parts.append("Return ONLY a JSON object:")
        parts.append('{"sql": "SELECT ...", "assumptions": [], "tables_used": [], "columns_used": [], "confidence": "high|medium|low", "reasoning_summary": "..."}')
        
        return "\n\n".join(parts)


# ── Main Facade ────────────────────────────────────────────────────────────

class SQLPatternMemory:
    """Main interface for SQL pattern memory system."""
    
    def __init__(self, db_path: str = "~/.hermes/sql_patterns.db"):
        self.store = SQLPatternStore(db_path)
        self.retriever = PatternRetriever(self.store)
        self.prompt_builder = FewShotPromptBuilder()
    
    def seed_from_bird(self, dev_path: str = None):
        """Seed the memory from BIRD-SQL dev set."""
        if dev_path is None:
            dev_path = os.path.join(
                os.path.dirname(__file__), "..",
                "bird_bench/dev/dev_20240627/dev.json"
            )
        tables_path = dev_path.replace("dev.json", "dev_tables.json")
        
        with open(dev_path) as f:
            dev_data = json.load(f)
        
        tables_data = None
        if os.path.exists(tables_path):
            with open(tables_path) as f:
                tables_data = json.load(f)
        
        print(f"Seeding pattern memory from {len(dev_data)} BIRD questions...")
        counts = self.retriever.seed_from_data(dev_data, tables_data)
        print(f"  Added: {counts['added']}, Skipped: {counts['skipped']}")
        print(f"  Total patterns: {self.store.count()}")
        
        # Show distribution
        conn = sqlite3.connect(self.store.db_path)
        rows = conn.execute(
            "SELECT query_type, COUNT(*) as cnt FROM patterns GROUP BY query_type ORDER BY cnt DESC"
        ).fetchall()
        print("  Distribution:")
        for r in rows:
            print(f"    {r[0]:25} {r[1]}")
        conn.close()
        
        return counts
    
    def retrieve(self, question: str, db_id: str, query_type: str = None,
                 schema_text: str = None, top_k: int = 3) -> list[PatternMatch]:
        """Retrieve matching patterns for a question."""
        if query_type is None:
            # We don't have the SQL yet, so we can't classify. Use the question instead.
            # Default to the most common type for this DB
            candidates = self.store.search(db_id=db_id, limit=10)
            if candidates:
                from collections import Counter
                type_counts = Counter(p.query_type for p in candidates)
                query_type = type_counts.most_common(1)[0][0]
            else:
                query_type = "simple_agg"
        
        return self.retriever.retrieve(question, db_id, query_type, top_k=top_k)
    
    def build_prompt(self, question: str, db_id: str, schema_text: str,
                     patterns: list[PatternMatch], evidence: str = None) -> str:
        """Build a full prompt with few-shot examples."""
        return self.prompt_builder.build(question, db_id, schema_text, patterns, evidence)
    
    def record_match(self, question: str, sql: str, db_id: str):
        """Record a successful match to boost pattern priority."""
        self.store.increment_match(question, sql, db_id)
    
    def add_pattern(self, question: str, sql: str, db_id: str, 
                    difficulty: str = "", query_type: str = None):
        """Add a new successful pattern manually."""
        if query_type is None:
            query_type = classify_query_type(sql)
        terms = extract_terms_from_question(question)
        pattern = SQLPattern(
            question=question, sql=sql, db_id=db_id,
            query_type=query_type, schema_hash="manual",
            terms=terms, difficulty=difficulty,
            match_count=1, created_at=time.time(),
        )
        self.store.add(pattern)
    
    def stats(self) -> dict:
        """Get memory statistics."""
        conn = sqlite3.connect(self.store.db_path)
        total = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
        by_type = dict(conn.execute(
            "SELECT query_type, COUNT(*) FROM patterns GROUP BY query_type"
        ).fetchall())
        by_db = dict(conn.execute(
            "SELECT db_id, COUNT(*) FROM patterns GROUP BY db_id"
        ).fetchall())
        conn.close()
        return {"total": total, "by_type": by_type, "by_db": by_db}


# ── Evaluation Runner with Pattern Memory ──────────────────────────────────

def evaluate_with_memory(
    dev_data: list[dict],
    memory: SQLPatternMemory,
    db_root: str,
    model_name: str = "deepseek-chat",
    reasoning_effort: str = "high",
    use_few_shot: bool = True,
    subset: int = None,
) -> dict:
    """Run SQL generation evaluation using pattern memory for few-shot."""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    
    from src.semantic_registry.pipeline.llm_gateway import DeepSeekProvider
    import time
    
    provider = DeepSeekProvider(model=model_name, reasoning_effort=reasoning_effort)
    
    if subset:
        dev_data = dev_data[:subset]
    
    results = []
    for i, q in enumerate(dev_data):
        db_id = q["db_id"]
        question = q["question"]
        gold_sql = q["SQL"]
        evidence = q.get("evidence", "")
        
        # Build schema text
        db_path = os.path.join(db_root, db_id, f"{db_id}.sqlite")
        
        # Get schema from the database
        schema_text = _get_schema_text(db_path)
        
        # Build prompt with or without few-shot
        if use_few_shot:
            qtype = classify_query_type(gold_sql)
            patterns = memory.retrieve(question, db_id, qtype, top_k=3)
            prompt = memory.build_prompt(question, db_id, schema_text, patterns, evidence)
        else:
            prompt = _build_basic_prompt(question, db_id, schema_text, evidence)
        
        # Generate
        try:
            raw = provider.generate(prompt)
            sql = _extract_sql(raw)
        except Exception as e:
            sql = ""
        
        # Evaluate
        match = False
        if sql:
            try:
                conn = sqlite3.connect(db_path)
                c = conn.cursor()
                c.execute(sql); pred = c.fetchall()
                c.execute(gold_sql); gold = c.fetchall()
                conn.close()
                match = set(pred) == set(gold)
            except:
                pass
        
        if match:
            memory.record_match(question, sql, db_id)
        
        results.append({
            "idx": i, "db_id": db_id, "difficulty": q["difficulty"],
            "match": match, "sql": sql, "gold": gold_sql,
        })
        
        if (i + 1) % 10 == 0:
            pct = sum(1 for r in results if r["match"]) / len(results) * 100
            print(f"  [{i+1}/{len(dev_data)}] Current EX: {pct:.1f}%", flush=True)
    
    passed = sum(1 for r in results if r["match"])
    return {"total": len(results), "passed": passed, "ex": passed / len(results) * 100 if results else 0}


def _get_schema_text(db_path: str) -> str:
    """Get CREATE TABLE statements from a SQLite database."""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL")
        schemas = [row[0] for row in cursor.fetchall()]
        conn.close()
        return "\n\n".join(schemas)
    except:
        return ""


def _build_basic_prompt(question: str, db_id: str, schema_text: str, evidence: str = None) -> str:
    parts = ["You are a SQLite expert. Generate a single SELECT statement."]
    parts.append("Database Schema:")
    parts.append(schema_text)
    if evidence:
        parts.append(f"Hint: {evidence}")
    parts.append(f"Question: {question}")
    parts.append("")
    parts.append('Return ONLY: {"sql": "SELECT ...", "assumptions": [], "tables_used": [], "columns_used": [], "confidence": "high|medium|low", "reasoning_summary": "..."}')
    return "\n\n".join(parts)


def _extract_sql(raw: str) -> str:
    """Extract SQL from LLM JSON response."""
    if not raw:
        return ""
    try:
        start = raw.find("{")
        if start >= 0:
            depth = 0
            in_str = False
            quote = ""
            for i in range(start, len(raw)):
                c = raw[i]
                if in_str:
                    if c == "\\": pass
                    elif c == quote: in_str = False
                elif c in ("'", '"'): in_str = True; quote = c
                elif c == "{": depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = raw[start:i+1]
                        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
                        data = json.loads(candidate)
                        if "sql" in data:
                            return data["sql"]
                        break
    except:
        pass
    
    # Fallback: look for SELECT
    m = re.search(r"SELECT\s+.*?(?:;|$)", raw, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(0).strip().rstrip(";")
    return ""


# ── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="SQL Pattern Memory System")
    parser.add_argument("--seed", action="store_true", help="Seed memory from BIRD")
    parser.add_argument("--stats", action="store_true", help="Show memory stats")
    parser.add_argument("--query", type=str, help="Test a query")
    parser.add_argument("--db", type=str, default="california_schools", help="DB for test query")
    args = parser.parse_args()
    
    memory = SQLPatternMemory()
    
    if args.seed:
        memory.seed_from_bird()
    
    if args.stats:
        s = memory.stats()
        print(f"Total patterns: {s['total']}")
        print(f"By type: {s['by_type']}")
        print(f"By DB: {s['by_db']}")
    
    if args.query:
        patterns = memory.retrieve(args.query, args.db, top_k=3)
        print(f"\nQuery: {args.query}")
        for i, m in enumerate(patterns):
            print(f"\nMatch {i+1} (score={m.score:.2f}):")
            print(f"  Question: {m.pattern.question}")
            print(f"  SQL: {m.pattern.sql}")
