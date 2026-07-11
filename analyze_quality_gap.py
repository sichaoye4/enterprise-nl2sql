"""Analyze the quality gap: why 289 compiled questions got EX=0?"""
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path.home() / "semantic_modeling" / "src"))
sys.path.insert(0, str(Path.home() / "enterprise-nl2sql"))

from semantic_engine.compiler.model_compiler import SemanticModelCompiler
from semantic_engine.loader.yaml_loader import load_semantic_model_file
from src.semantic_registry.pipeline.semantic_router import RouterResult, compile_from_router

BIRD_DIR = Path.home() / "enterprise-nl2sql" / "bird_bench" / "dev" / "dev_20240627"
MODEL_DIR = Path.home() / "enterprise-nl2sql" / "bird_semantic_engine"
DB_DIR = BIRD_DIR / "databases" / "dev_databases"

with open(BIRD_DIR / "dev.json") as f:
    all_questions = json.load(f)


def find_measure(sql, snapshot):
    upper = sql.upper()
    agg = re.search(r"(SUM|COUNT|AVG|MAX|MIN)\s*\(\s*(?:\w+\.)?(\w+)", upper)
    if not agg:
        return None
    func, col = agg.group(1), agg.group(2).lower()
    if col == "*":
        for e in snapshot.entities.values():
            for m in e.measures:
                if m.aggregation and m.aggregation.upper() == "COUNT":
                    return f"{e.name}.{m.name}"
        return None
    for e in snapshot.entities.values():
        for m in e.measures:
            mc = str(m.expr).lower().split(".")[-1].strip() if m.expr else ""
            if col in mc or col == mc:
                if m.aggregation and m.aggregation.upper() == func:
                    return f"{e.name}.{m.name}"
    return None


def extract_filters(sql, snapshot):
    filters = []
    where = re.search(r"WHERE\s+(.+?)(?:\s+ORDER\s|\s+LIMIT\s|\s+GROUP\s|$)", sql, re.IGNORECASE | re.DOTALL)
    if not where:
        return filters
    col_map = {}
    for e in snapshot.entities.values():
        for d in list(e.dimensions) + list(e.identifiers):
            c = str(d.expr).split(".")[-1].strip().lower() if d.expr else ""
            col_map[c] = f"{e.name}.{d.name}"
    clauses = re.split(r"\s+AND\s+", where.group(1).strip())
    for c in clauses:
        c = c.strip()
        for pattern, op in [
            (r"(\w+)\.(\w+)\s+LIKE\s+'([^']+)'", "like"),
            (r"(\w+)\.(\w+)\s+BETWEEN\s+(.+?)\s+AND\s+(.+)", "between"),
            (r"(\w+)\.(\w+)\s*=\s*'([^']+)'", "equals"),
        ]:
            m = re.match(pattern, c, re.IGNORECASE)
            if m:
                col = m.group(2).lower()
                if col in col_map:
                    vals = [v.strip().strip("'\"") for v in m.groups()[2:-1]] if op == "between" else [m.group(3).strip().strip("'\"")]
                    filters.append({"member": col_map[col], "operator": op, "values": vals})
                break
    return filters


def classify_failure(gold_sql, compiled_sql, gold_rows, our_rows):
    """Classify why a compiled query failed."""
    upper = gold_sql.upper()
    
    # Check gold SQL complexity
    reasons = []
    if "JOIN " in upper:
        reasons.append("gold_has_join")
    if "IIF(" in upper or "CASE WHEN" in upper:
        reasons.append("gold_has_case_when")
    if re.search(r"SELECT\s+(?:DISTINCT\s+)?\w+\.", upper) and "FROM" in upper and "WHERE" not in upper:
        # Simple SELECT with no WHERE
        pass
    if re.search(r"LIMIT\s+\d+", upper, re.IGNORECASE):
        reasons.append("gold_has_limit")
    if re.search(r"ORDER\s+BY", upper, re.IGNORECASE):
        reasons.append("gold_has_order_by")
    if re.search(r"\bTOP\s+\d+\b", upper, re.IGNORECASE):
        reasons.append("gold_has_top")
    
    # Check compiled SQL
    if compiled_sql:
        comp_upper = compiled_sql.upper()
        if "LEFT JOIN" in comp_upper and "WHERE" in comp_upper:
            reasons.append("join_path_added")
        if "GROUP BY" in comp_upper:
            reasons.append("group_by_added")
    
    # Check data mismatch
    if gold_rows and our_rows:
        if len(gold_rows) != len(our_rows):
            reasons.append(f"row_count_mismatch({len(gold_rows)}vs{len(our_rows)})")
        elif gold_rows and our_rows and str(gold_rows[0]) != str(our_rows[0]):
            reasons.append("value_mismatch")
    
    return ", ".join(reasons) if reasons else "unknown"


failure_patterns = Counter()
per_db = {}

for db_id in sorted(set(q["db_id"] for q in all_questions)):
    db_qs = [q for q in all_questions if q["db_id"] == db_id]
    snapshot_path = MODEL_DIR / db_id / "model.yml"
    db_path = DB_DIR / db_id / f"{db_id}.sqlite"
    
    if not snapshot_path.exists() or not db_path.exists():
        continue
    
    snapshot = SemanticModelCompiler().compile(load_semantic_model_file(snapshot_path))
    conn = sqlite3.connect(str(db_path))
    
    db_patterns = Counter()
    
    for q in db_qs:
        measure = find_measure(q["SQL"], snapshot)
        if not measure:
            continue
        
        filters = extract_filters(q["SQL"], snapshot)
        try:
            result = RouterResult(measure=measure, dimensions=[], time_dimension=None, granularity=None, filters=filters, confidence=0.95)
            compiled = compile_from_router(snapshot, result, q["question"])
        except:
            continue
        
        if compiled is None:
            continue
        if "DATE_TRUNC" in compiled.sql.upper() or "NOW()" in compiled.sql.upper():
            continue
        
        try:
            sql_sqlite = compiled.sql.replace("%s", "?")
            cursor = conn.execute(sql_sqlite, compiled.parameters)
            our_rows = cursor.fetchall()
        except:
            continue
        
        try:
            cursor = conn.execute(q["SQL"], [])
            gold_rows = cursor.fetchall()
        except:
            continue
        
        # Normalize comparison
        our_set = set(tuple(str(v).strip() for v in r) for r in our_rows)
        gold_set = set(tuple(str(v).strip() for v in r) for r in gold_rows)
        
        if our_set != gold_set:
            pattern = classify_failure(q["SQL"], compiled.sql, gold_rows, our_rows)
            failure_patterns[pattern] += 1
            db_patterns[pattern] += 1
    
    conn.close()
    if db_patterns:
        per_db[db_id] = db_patterns

print("=" * 72)
print("  Quality Gap Analysis: Why 289 compiled queries got EX=0?")
print("=" * 72)
print()
print("  Top failure patterns across ALL databases:")
print()
for pattern, count in failure_patterns.most_common():
    pct = count / sum(failure_patterns.values()) * 100
    print(f"  {pct:5.1f}% ({count:3d})  {pattern}")

print()
print("=" * 72)
print("  Per-database breakdown:")
print()
for db_id in sorted(per_db.keys()):
    dbp = per_db[db_id]
    total = sum(dbp.values())
    print(f"  {db_id} ({total} failures):")
    for p, c in dbp.most_common(3):
        print(f"    {c:3d}  {p}")
    print()
