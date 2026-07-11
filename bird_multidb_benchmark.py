"""
Comprehensive multi-DB BIRD benchmark: semantic engine mock router across all 11 databases.
Tests routability, compilation success, and execution accuracy.
"""
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path.home() / "semantic_modeling" / "src"))
sys.path.insert(0, str(Path.home() / "enterprise-nl2sql"))

from semantic_engine.compiler.model_compiler import SemanticModelCompiler
from semantic_engine.loader.yaml_loader import load_semantic_model_file
from src.semantic_registry.pipeline.semantic_router import RouterResult, compile_from_router, _members_by_type

BIRD_DIR = Path.home() / "enterprise-nl2sql" / "bird_bench" / "dev" / "dev_20240627"
MODEL_DIR = Path.home() / "enterprise-nl2sql" / "bird_semantic_engine"
DEV_JSON = BIRD_DIR / "dev.json"
DB_DIR = BIRD_DIR / "databases" / "dev_databases"
PG_ONLY = {"DATE_TRUNC", "NOW()"}


def load_questions():
    with open(DEV_JSON) as f:
        return json.load(f)


def run_sql(db_path, sql, params):
    conn = sqlite3.connect(str(db_path))
    try:
        sql = sql.replace("%s", "?")
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall()
        desc = [d[0] for d in cursor.description]
        return desc, rows
    except Exception as e:
        return None, str(e)
    finally:
        conn.close()


def normalize(rows):
    if not rows:
        return set()
    return set(tuple(str(v).strip() for v in row) for row in rows)


def ex(result, gold):
    if result is None or gold is None:
        return 0.0
    r, g = normalize(result), normalize(gold)
    if not g:
        return 1.0 if not r else 0.0
    return 1.0 if r == g else 0.0


def find_measure(sql, snapshot):
    """Find matching measure from gold SQL."""
    upper = sql.upper()
    agg = re.search(r"(SUM|COUNT|AVG|MAX|MIN)\s*\(\s*(?:\w+\.)?(\w+)", upper)
    if not agg:
        return None, "no aggregation"
    func, col = agg.group(1), agg.group(2).lower()
    if col == "*":
        for e in snapshot.entities.values():
            for m in e.measures:
                if m.aggregation and m.aggregation.upper() == "COUNT":
                    return f"{e.name}.{m.name}", "count_star"
        return None, "count_star_unmatched"
    
    candidates = []
    for e in snapshot.entities.values():
        for m in e.measures:
            mc = str(m.expr).lower().split(".")[-1].strip() if m.expr else ""
            if col in mc or col == mc:
                if m.aggregation and m.aggregation.upper() == func:
                    return f"{e.name}.{m.name}", "exact"
                candidates.append((f"{e.name}.{m.name}", m.aggregation.upper() if m.aggregation else "NONE"))
    if candidates:
        return candidates[0][0], f"agg_mismatch(wanted={func},got={candidates[0][1]})"
    return None, f"no_measure_for_{col}"


def extract_filters(sql, snapshot):
    """Extract filters from gold SQL WHERE clause."""
    filters = []
    where = re.search(r"WHERE\s+(.+?)(?:\s+ORDER\s|\s+LIMIT\s|\s+GROUP\s|$)", sql, re.IGNORECASE | re.DOTALL)
    if not where:
        return filters
    
    # Build column -> member lookup
    col_map = {}
    for e in snapshot.entities.values():
        for d in list(e.dimensions) + list(e.identifiers):
            c = str(d.expr).split(".")[-1].strip().lower() if d.expr else ""
            col_map[c] = f"{e.name}.{d.name}"
    
    clauses = re.split(r"\s+AND\s+", where.group(1).strip())
    for c in clauses:
        c = c.strip()
        # Try each pattern
        for pattern, op in [
            (r"(\w+)\.(\w+)\s+LIKE\s+'([^']+)'", "like"),
            (r"(\w+)\.(\w+)\s+BETWEEN\s+(.+?)\s+AND\s+(.+)", "between"),
            (r"(\w+)\.(\w+)\s*=\s*'([^']+)'", "equals"),
            (r"(\w+)\.(\w+)\s*>\s*'?([^']+)'?", "gt"),
            (r"(\w+)\.(\w+)\s*<\s*'?([^']+)'?", "lt"),
            (r"(\w+)\.(\w+)\s*>=\s*'?([^']+)'?", "gte"),
            (r"(\w+)\.(\w+)\s*<=\s*'?([^']+)'?", "lte"),
        ]:
            m = re.match(pattern, c, re.IGNORECASE)
            if m:
                col = m.group(2).lower()
                if col in col_map:
                    vals = [v.strip().strip("'\"") for v in m.groups()[2:-1]] if op == "between" else [m.group(3).strip().strip("'\"")]
                    filters.append({"member": col_map[col], "operator": op, "values": vals})
                break
    return filters


def benchmark_db(db_id, questions, snapshot, db_path):
    """Run mock router benchmark on one database. Returns detailed results."""
    agg_types = defaultdict(int)
    total = len(questions)
    results = []
    
    for q in questions:
        sql = q["SQL"]
        upper = sql.upper()
        
        # Classify the gold SQL
        tags = []
        if re.search(r"(SUM|COUNT|AVG|MAX|MIN)\s*\(", upper):
            tags.append("aggregation")
        if "IIF(" in upper or "CASE WHEN" in upper:
            tags.append("case_when")
        if "JOIN " in upper:
            tags.append("join")
        if "SELECT" in upper[upper.index("FROM") + 5:] if "FROM" in upper else False:
            tags.append("subquery")
        if "WITH " in upper:
            tags.append("cte")
        agg_types[", ".join(tags) if tags else "other"] += 1
        
        # Find measure
        measure, reason = find_measure(sql, snapshot)
        if not measure:
            results.append((q, "no_measure", None, None, reason))
            continue
        
        # Extract filters, compile
        filters = extract_filters(sql, snapshot)
        try:
            router_res = RouterResult(measure=measure, dimensions=[], time_dimension=None, granularity=None, filters=filters, confidence=0.95)
            compiled = compile_from_router(snapshot, router_res, q["question"])
        except Exception:
            results.append((q, "compile_error", None, None, "exception"))
            continue
        
        if compiled is None:
            results.append((q, "compile_failed", None, None, ""))
            continue
        
        # Check PG-only
        if any(fn in compiled.sql.upper() for fn in PG_ONLY):
            results.append((q, "pg_only", compiled.sql, None, ""))
            continue
        
        # Execute
        desc, exec_out = run_sql(db_path, compiled.sql, compiled.parameters)
        if desc is None:
            results.append((q, "exec_failed", compiled.sql, None, exec_out[:80]))
            continue
        
        # Compare with gold
        _, gold_out = run_sql(db_path, sql, [])
        ex_score = ex(exec_out, gold_out)
        results.append((q, "ok", compiled.sql, ex_score, ""))
    
    return results, dict(agg_types), total


def print_db_results(db_id, results, agg_types, total):
    """Print formatted results for one database."""
    counts = defaultdict(int)
    ex_scores = []
    
    for r in results:
        counts[r[1]] += 1
        if r[1] == "ok" and isinstance(r[3], (int, float)):
            ex_scores.append(r[3])
    
    routable = counts["ok"] + counts.get("no_measure", 0) + counts.get("compile_error", 0) + counts.get("compile_failed", 0) + counts.get("exec_failed", 0) + counts.get("pg_only", 0)
    avg_ex = sum(ex_scores) / len(ex_scores) if ex_scores else 0.0
    perfect = sum(1 for s in ex_scores if s == 1.0)
    
    print(f"\n{'─' * 72}")
    print(f"  {db_id}")
    print(f"{'─' * 72}")
    print(f"  Total questions:      {total}")
    print(f"  Measure matched:      {routable}/{total} ({routable/total*100:.0f}%)")
    print(f"  Compiled + executed:  {len(ex_scores)}/{routable} ({len(ex_scores)/routable*100:.0f}%)")
    print(f"  No matching measure:  {counts.get('no_measure', 0)}")
    print(f"  PG-only (DATE_TRUNC): {counts.get('pg_only', 0)}")
    print(f"  Compile failures:     {counts.get('compile_failed', 0) + counts.get('compile_error', 0)}")
    print(f"  Execution failures:   {counts.get('exec_failed', 0)}")
    print(f"  Execution Accuracy:   {avg_ex*100:.1f}% ({perfect}/{len(ex_scores)} perfect)")
    
    if results:
        print(f"\n  Top-5 failures (worst EX):")
        failed = sorted([r for r in results if r[1] == "ok" and isinstance(r[3], (int, float)) and r[3] < 1.0], key=lambda x: x[3])
        for r in failed[:5]:
            print(f"    [{r[0]['question_id']}] EX={r[3]:.2f} — {r[0]['question'][:55]}")
    
    return {
        "db_id": db_id,
        "total": total,
        "routable": routable,
        "executed": len(ex_scores),
        "ex_avg": avg_ex,
        "ex_perfect": perfect,
        "no_measure": counts.get("no_measure", 0),
        "pg_only": counts.get("pg_only", 0),
        "compile_fail": counts.get("compile_failed", 0) + counts.get("compile_error", 0),
        "exec_fail": counts.get("exec_failed", 0),
    }


def main():
    print("=" * 72)
    print("  BIRD Multi-DB Semantic Engine Benchmark")
    print("  11 databases, 1534 questions")
    print("  Mock router: ideal filters from gold SQL")
    print("=" * 72)
    
    questions = load_questions()
    all_db_ids = sorted(set(q["db_id"] for q in questions))
    
    print(f"\n  Databases: {len(all_db_ids)}")
    for db in all_db_ids:
        count = sum(1 for q in questions if q["db_id"] == db)
        print(f"    {db}: {count} questions")
    
    summaries = []
    grand_total = 0
    grand_executed = 0
    grand_ex_scores = []
    grand_perfect = 0
    grand_routable = 0
    grand_no_measure = 0
    
    for db_id in all_db_ids:
        print(f"\n{'=' * 72}")
        print(f"  Loading: {db_id}")
        
        snapshot_path = MODEL_DIR / db_id / "model.yml"
        if not snapshot_path.exists():
            print(f"  SKIPPED: no model at {snapshot_path}")
            continue
        
        # Load snapshot
        snapshot = SemanticModelCompiler().compile(load_semantic_model_file(snapshot_path))
        
        db_path = DB_DIR / db_id / f"{db_id}.sqlite"
        if not db_path.exists():
            print(f"  SKIPPED: no DB at {db_path}")
            continue
        
        db_qs = [q for q in questions if q["db_id"] == db_id]
        
        print(f"  Model: {snapshot.snapshot_id}")
        print(f"  Entities: {len(snapshot.entities)}, Catalog: {len(snapshot.catalog_index)}")
        print(f"  Questions: {len(db_qs)}")
        
        results, agg_types, total = benchmark_db(db_id, db_qs, snapshot, db_path)
        summary = print_db_results(db_id, results, agg_types, total)
        summaries.append(summary)
        
        # Aggregate
        grand_total += summary["total"]
        grand_routable += summary["routable"]
        grand_executed += summary["executed"]
        grand_ex_scores.append(summary["ex_avg"] * summary["executed"])
        grand_perfect += summary["ex_perfect"]
        grand_no_measure += summary["no_measure"]
    
    # Grand summary
    total_executed = sum(s["executed"] for s in summaries)
    total_ex = sum(s["ex_avg"] * s["executed"] for s in summaries)
    grand_avg_ex = total_ex / total_executed if total_executed else 0.0
    grand_routable_total = sum(s["routable"] for s in summaries)
    grand_perfect_total = sum(s["ex_perfect"] for s in summaries)
    
    print(f"\n{'=' * 72}")
    print(f"  GRAND SUMMARY — All {len(summaries)} Databases")
    print(f"{'=' * 72}")
    print(f"  Total questions:       {grand_total}")
    print(f"  Measure matched:       {grand_routable_total}/{grand_total} ({grand_routable_total/grand_total*100:.1f}%)")
    print(f"  Compiled + executed:   {total_executed}/{grand_routable_total} ({total_executed/grand_routable_total*100:.0f}%)")
    print(f"  No matching measure:   {grand_no_measure}")
    print(f"  Execution Accuracy:    {grand_avg_ex*100:.1f}% ({grand_perfect_total}/{total_executed} perfect)")
    print(f"\n  {'DB':<30} {'Total':<7} {'Routable':<10} {'Executed':<10} {'EX%':<7} {'Perfect':<8}")
    print(f"  {'-'*30} {'-'*7} {'-'*10} {'-'*10} {'-'*7} {'-'*8}")
    for s in sorted(summaries, key=lambda x: x["ex_avg"], reverse=True):
        ex_pct = s["ex_avg"] * 100
        print(f"  {s['db_id']:<30} {s['total']:<7} {s['routable']:<10} {s['executed']:<10} {ex_pct:<7.1f} {s['ex_perfect']:<8}")
    
    print(f"\n  {'TOTAL':<30} {grand_total:<7} {grand_routable_total:<10} {total_executed:<10} {grand_avg_ex*100:<7.1f} {grand_perfect_total:<8}")
    print(f"{'=' * 72}")
    
    # Analysis
    print(f"\n{'=' * 72}")
    print(f"  ANALYSIS")
    print(f"{'=' * 72}")
    print(f"")
    print(f"  Semantic Engine Role:")
    print(f"  ───────────────────")
    print(f"  The semantic engine provides deterministic SQL compilation for")
    print(f"  questions that match governed measures. When a match is found,")
    print(f"  the SQL is guaranteed to be syntactically valid and uses only")
    print(f"  approved tables/columns.")
    print(f"")
    print(f"  Upper bound (mock router): {grand_avg_ex*100:.1f}% EX on {total_executed}/{grand_total} questions")
    print(f"  This is the BEST the semantic engine can do (ideal filters).")
    print(f"  Real LLM router will score lower due to filter extraction errors.")
    print(f"")
    print(f"  Coverage gap: {grand_total - grand_routable_total} questions have no matching measure")
    print(f"  These MUST fall through to LLM SQL generation.")
    print(f"")
    print(f"  Quality gap: {total_executed - grand_perfect_total} compiled questions got EX<1.0")
    print(f"  These have measures but produce wrong results (wrong join path,")
    print(f"  measure mismatch, filter mapping errors).")
    print(f"{'=' * 72}\n")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
