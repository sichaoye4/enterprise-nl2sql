"""
Comprehensive BIRD benchmark: NL2SQL pipeline with LLM router.
Tests routability, compilation success, and execution accuracy.

Modes:
  mock  — ideal router (gold-SQL-derived filters), tests max potential
  real  — actual LLM router call (uses OpenRouter), tests real performance
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "semantic_modeling" / "src"))
sys.path.insert(0, str(Path.home() / "enterprise-nl2sql"))

from semantic_engine.compiler.model_compiler import SemanticModelCompiler
from semantic_engine.loader.yaml_loader import load_semantic_model_file
from src.semantic_registry.pipeline.semantic_router import (
    SemanticRouter,
    compile_from_router,
    RouterResult,
    SUPPORTED_FILTER_OPERATORS,
    _members_by_type,
    _strip_measure_filters,
)

BIRD_DIR = Path.home() / "enterprise-nl2sql" / "bird_bench" / "dev" / "dev_20240627"
DB_ID = "debit_card_specializing"
DB_PATH = BIRD_DIR / "databases" / "dev_databases" / DB_ID / f"{DB_ID}.sqlite"
DEV_JSON = BIRD_DIR / "dev.json"
MODEL_DIR = Path.home() / "enterprise-nl2sql" / "bird_semantic_engine" / DB_ID


def load_snapshot():
    path = MODEL_DIR / "model.yml"
    return SemanticModelCompiler().compile(load_semantic_model_file(path))


def load_questions():
    with open(DEV_JSON) as f:
        all_qs = json.load(f)
    return [q for q in all_qs if q.get("db_id") == DB_ID]


PG_ONLY_FUNCTIONS = {"DATE_TRUNC", "NOW()"}


def run_sql(sql, params):
    """Run SQL against SQLite, converting PostgreSQL placeholders."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        sql_sqlite = sql.replace("%s", "?")
        cursor = conn.execute(sql_sqlite, params)
        rows = cursor.fetchall()
        desc = [d[0] for d in cursor.description]
        return desc, rows
    except Exception as e:
        return None, str(e)
    finally:
        conn.close()


def classify_sql(sql):
    """Classify gold SQL by complexity type."""
    upper = sql.upper()
    tags = []
    if "WITH " in upper:
        tags.append("cte")
    if "OVER (" in upper or "ROW_NUMBER" in upper:
        tags.append("window")
    if "JOIN " in upper:
        tags.append("join")
    if "CASE WHEN" in upper or "IIF(" in upper:
        tags.append("case_when")
    if "CAST(" in upper and "/" in upper:
        tags.append("ratio")
    if "SELECT DISTINCT" in upper or "SELECT T1." in upper:
        tags.append("multi_table")
    if "SELECT" in upper and re.search(r"\b(SUM|COUNT|AVG|MAX|MIN)\s*\(", upper):
        tags.append("aggregation")
    if "SUBSTRING" in upper or "LIKE" in upper or "STRFTIME" in upper:
        tags.append("string_op")
    if "LIMIT" in upper or "TOP" in upper:
        tags.append("top_n")
    if "DISTINCT" in upper:
        tags.append("distinct")
    return tags


def analyze_routability(sql, snapshot):
    """
    Determine if a gold SQL *could* be expressed via the governed model.
    Returns: 'routable', 'partial', or 'not_routable' with reason.
    """
    upper = sql.upper()
    tags = classify_sql(sql)
    
    # These patterns CANNOT be expressed via single-measure + filters
    non_routable_patterns = [
        "WITH ",
        "OVER (",
        "ROW_NUMBER",
        "SELECT DISTINCT",
        "SUBSTRING",
        "STRFTIME",
        "IIF(",
        "CAST(",
    ]
    
    for pattern in non_routable_patterns:
        if pattern in upper:
            return "not_routable", f"requires {pattern.strip()} (non-measure SQL)"
    
    # Ratio requires two measures
    if "CASE WHEN" in upper and "/" in upper:
        return "not_routable", "requires ratio calculation (two measures)"
    
    # Multi-table JOINs = requires measures from different entities
    if "JOIN " in upper and not "LEFT JOIN" in upper:
        # Check if it's just a simple dimension join
        # Simple: SELECT agg(col) FROM table WHERE filter
        if re.search(r"JOIN\s+\w+\s+(AS\s+)?\w+\s+ON", upper, re.IGNORECASE):
            agg_match = re.search(r"\b(SUM|COUNT|AVG|MAX|MIN)\s*\(\s*T\d\.(\w+)", upper)
            if agg_match:
                # It uses an alias T1, T2 pattern - could be multi-entity
                return "partial", f"multi-table join (may still be expressible)"
            return "not_routable", "multi-table join"
    
    # Simple aggregation with WHERE filters = most routable
    agg_match = re.search(r"SELECT\s+(SUM|COUNT|AVG|MAX|MIN)\s*\((.+?)\)", upper)
    if agg_match:
        agg_func = agg_match.group(1)
        col_ref = agg_match.group(2).strip()
        
        # Check if the measure exists in the snapshot
        for entity in snapshot.entities.values():
            for measure in entity.measures:
                measure_col = str(measure.expr) if measure.expr else ""
                if measure_col.lower() in col_ref.lower() or col_ref.lower() in measure_col.lower():
                    # Check if the aggregate function matches
                    if measure.aggregation and measure.aggregation.upper() == agg_func:
                        return "routable", f"measure {entity.name}.{measure.name}"
        
        # Check if the target column exists as an identifier or dimension
        col_name = col_ref.split(".")[-1].strip()
        for entity in snapshot.entities.values():
            for identifier in entity.identifiers:
                id_col = str(identifier.expr) if identifier.expr else ""
                if col_name.lower() == id_col.lower() or col_name.lower() == identifier.name.lower():
                    return "partial", f"column {col_name} is an identifier, not a measure"
            for dimension in entity.dimensions:
                dim_col = str(dimension.expr) if dimension.expr else ""
                if col_name.lower() == dim_col.lower():
                    return "partial", f"column {col_name} is a dimension, not a measure"
        
        return "routable", f"simple {agg_func.lower()} aggregation"
    
    return "not_routable", "no aggregation found"


def extract_filters_from_gold(sql, snapshot):
    """Extract filter member/operator/value from gold SQL WHERE clause."""
    filters = []
    where_match = re.search(r"WHERE\s+(.+?)(?:\s+ORDER\s|\s+LIMIT\s|\s+GROUP\s|$)", sql, re.IGNORECASE | re.DOTALL)
    if not where_match:
        return filters
    
    where_clause = where_match.group(1).strip()
    
    # Build a lookup: column expr -> qualified name
    col_to_member = {}
    for entity in snapshot.entities.values():
        for dim in entity.dimensions:
            col = str(dim.expr).split(".")[-1].strip() if dim.expr else ""
            col_to_member[col.lower()] = f"{entity.name}.{dim.name}"
        for ident in entity.identifiers:
            col = str(ident.expr).split(".")[-1].strip() if ident.expr else ""
            col_to_member[col.lower()] = f"{entity.name}.{ident.name}"
        for td in entity.time_dimensions:
            col = str(td.expr).split(".")[-1].strip() if td.expr else ""
            col_to_member[col.lower()] = f"{entity.name}.{td.name}"
    
    # Parse simple conditions: column = 'value' or column LIKE 'pattern'
    # Handle AND-separated conditions
    conditions = re.split(r"\s+AND\s+", where_clause)
    for cond in conditions:
        cond = cond.strip()
        
        # LIKE pattern: column LIKE 'pattern'
        like_match = re.match(r"(\w+)\.(\w+)\s+LIKE\s+'(.+?)'", cond, re.IGNORECASE)
        if like_match:
            col = like_match.group(2).lower()
            if col in col_to_member:
                filters.append({
                    "member": col_to_member[col], "operator": "like",
                    "values": [like_match.group(3)]
                })
            continue
        
        # BETWEEN: column BETWEEN value AND value
        between_match = re.match(r"(\w+)\.(\w+)\s+BETWEEN\s+(.+?)\s+AND\s+(.+)", cond, re.IGNORECASE)
        if between_match:
            col = between_match.group(2).lower()
            if col in col_to_member:
                v1 = between_match.group(3).strip().strip("'\"")
                v2 = between_match.group(4).strip().strip("'\"")
                filters.append({
                    "member": col_to_member[col], "operator": "between",
                    "values": [v1, v2]
                })
            continue
        
        # Equals: column = 'value'
        eq_match = re.match(r"(\w+)\.(\w+)\s*=\s*'(.+?)'", cond, re.IGNORECASE)
        if eq_match:
            col = eq_match.group(2).lower()
            if col in col_to_member:
                filters.append({
                    "member": col_to_member[col], "operator": "equals",
                    "values": [eq_match.group(3)]
                })
            continue
        
        # >, <, >=, <=
        comp_match = re.match(r"(\w+)\.(\w+)\s*(>=|<=|>|<)\s*(.+)", cond, re.IGNORECASE)
        if comp_match:
            col = comp_match.group(2).lower()
            op_map = {">": "gt", "<": "lt", ">=": "gte", "<=": "lte"}
            op = op_map.get(comp_match.group(3), "gt")
            val = comp_match.group(4).strip().strip("'\"")
            if col in col_to_member:
                filters.append({
                    "member": col_to_member[col], "operator": op,
                    "values": [val]
                })
    
    return filters


def find_measure_for_gold(sql, snapshot):
    """Find the best matching measure in the snapshot for a gold SQL."""
    upper = sql.upper()
    # Try with table alias: COUNT(T1.Column)
    agg_match = re.search(r"(SUM|COUNT|AVG|MAX|MIN)\s*\(\s*(?:\w+\.)?(\w+)", upper)
    if not agg_match:
        return None, "no aggregation"
    agg_func = agg_match.group(1)
    col_name = agg_match.group(2).lower()
    
    # Also handle: COUNT(*) → no specific column
    if col_name == "*":
        # For COUNT(*), try to find any COUNT measure in any entity
        for entity in snapshot.entities.values():
            for measure in entity.measures:
                if measure.aggregation and measure.aggregation.upper() == "COUNT":
                    return f"{entity.name}.{measure.name}", "count_star"
        return None, "count_star_no_match"
    
    candidates = []
    for entity in snapshot.entities.values():
        for measure in entity.measures:
            measure_col = str(measure.expr).lower() if measure.expr else ""
            # Handle both fully qualified (t0.col) and simple (col)
            measure_col_short = measure_col.split(".")[-1].strip()
            if col_name in measure_col or col_name == measure_col_short:
                if measure.aggregation and measure.aggregation.upper() == agg_func:
                    return f"{entity.name}.{measure.name}", "exact"
                candidates.append((f"{entity.name}.{measure.name}", measure.aggregation.upper() if measure.aggregation else "NONE"))
    if candidates:
        return candidates[0][0], f"agg_mismatch (wanted {agg_func}, got {candidates[0][1]})"
    return None, f"no measure for column {col_name}"


def normalize_results(rows):
    if not rows:
        return set()
    normed = set()
    for row in rows:
        normed.add(tuple(str(v).strip() for v in row))
    return normed


def safe_ex(result_rows, gold_rows):
    """Compare results as string sets."""
    if result_rows is None or gold_rows is None:
        return 0.0
    r_set = normalize_results(result_rows)
    g_set = normalize_results(gold_rows)
    if not g_set:
        return 1.0 if not r_set else 0.0
    return 1.0 if r_set == g_set else 0.0


def analyze_gold_sqls(questions, snapshot):
    """Categorize all gold SQLs by routability."""
    categories = {"routable": [], "partial": [], "not_routable": []}
    for q in questions:
        routability, reason = analyze_routability(q["SQL"], snapshot)
        categories[routability].append((q, reason))
    return categories


def mock_router_benchmark(questions, snapshot):
    """Run mock router on routable candidates. Uses ideal filters from gold SQL."""
    results = []
    for q in questions:
        try:
            sql = q["SQL"]
            measure_name, reason = find_measure_for_gold(sql, snapshot)
            if not measure_name:
                continue
            
            filters = extract_filters_from_gold(sql, snapshot)
            
            result = RouterResult(
                measure=measure_name,
                dimensions=[],
                time_dimension=None,
                granularity=None,
                filters=filters,
                confidence=0.95,
            )
            
            compiled = compile_from_router(snapshot, result, q["question"])
            
            if compiled is None:
                results.append((q, "compile_failed", None, None))
                continue
            
            # Execute against SQLite
            upper_sql = compiled.sql.upper()
            if any(fn in upper_sql for fn in PG_ONLY_FUNCTIONS):
                results.append((q, "pg_only", compiled.sql, "PG function"))
                continue
            
            desc, exec_result = run_sql(compiled.sql, compiled.parameters)
            
            if desc is None:
                results.append((q, "exec_failed", compiled.sql, str(exec_result)))
                continue
            
            # Run gold SQL
            _, gold_result = run_sql(sql, [])
            
            ex = safe_ex(exec_result, gold_result)
            results.append((q, "ok", compiled.sql, ex))
        except Exception as e:
            results.append((q, "error", None, str(e)[:100]))
    return results


def real_llm_benchmark(questions, snapshot):
    """Run a subset through the real LLM router."""
    import requests, os
    
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("WARNING: No OPENROUTER_API_KEY set. Skipping real LLM benchmark.")
        return []
    
    results = []
    call_count = [0]
    
    def llm_generate(prompt):
        call_count[0] += 1
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 300,
        }
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload, timeout=30
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
        except Exception:
            pass
        return '{"measure": "", "confidence": 0.0}'
    
    router = SemanticRouter(snapshot, llm_generate)
    
    for q in questions:
        print(f"  LLM routing: {q['question'][:60]}...", end=" ")
        sys.stdout.flush()
        
        result = router.route(q["question"], db_id=DB_ID)
        if result is None:
            print("NO MATCH")
            results.append((q, "no_match", None, None))
            continue
        
        compiled = compile_from_router(snapshot, result, q["question"])
        if compiled is None:
            print("COMPILE FAILED")
            results.append((q, "compile_failed", None, None))
            continue
        
        desc, exec_result = run_sql(compiled.sql, compiled.parameters)
        if desc is None:
            print(f"EXEC FAILED: {exec_result}")
            results.append((q, "exec_failed", compiled.sql, str(exec_result)))
            continue
        
        _, gold_result = run_sql(q["SQL"], [])
        ex = safe_ex(exec_result, gold_result)
        
        print(f"EX={ex:.2f} | {compiled.sql[:70]}")
        results.append((q, "ok", compiled.sql, ex))
    
    print(f"\n  Total LLM calls: {call_count[0]}")
    return results


def print_results_table(all_results, title):
    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print(f"{'=' * 72}")
    
    if not all_results:
        print("  No results.")
        return
    
    total = len(all_results)
    ok = sum(1 for r in all_results if r[1] == "ok")
    compile_fail = sum(1 for r in all_results if r[1] == "compile_failed")
    exec_fail = sum(1 for r in all_results if r[1] == "exec_failed")
    pg_only = sum(1 for r in all_results if r[1] == "pg_only")
    no_match = sum(1 for r in all_results if r[1] == "no_match")
    errors = sum(1 for r in all_results if r[1] == "error")
    
    ex_scores = [r[3] for r in all_results if r[1] == "ok" and isinstance(r[3], (int, float))]
    avg_ex = sum(ex_scores) / len(ex_scores) if ex_scores else 0.0
    
    print(f"  Total: {total}")
    print(f"  Executed successfully: {ok} ({ok/total*100:.0f}%)")
    print(f"  Execution failures: {exec_fail}")
    print(f"  PostgreSQL-only (skipped): {pg_only}")
    print(f"  Compile failures: {compile_fail}")
    print(f"  No router match: {no_match}")
    print(f"  Errors: {errors}")
    print(f"  Execution accuracy (EX): {avg_ex*100:.1f}% ({sum(ex_scores)}/{len(ex_scores)})")
    
    print(f"  {'QID':<6} {'Status':<16} {'Question':<42} {'EX':<6}")
    print(f"  {'-'*6} {'-'*16} {'-'*42} {'-'*6}")
    for r in all_results:
        q = r[0]
        status = r[1]
        ex = r[3]
        if isinstance(ex, (int, float)):
            ex_str = f"{ex*100:.0f}%"
        else:
            ex_str = str(ex)[:6] if ex else status[:6]
        print(f"  {q['question_id']:<6} {status:<16} {q['question'][:40]:<42} {ex_str:<6}")


def main():
    print(f"{'=' * 72}")
    print(f"  BIRD NL2SQL Benchmark — {DB_ID}")
    print(f"  Path: {DB_PATH}")
    print(f"  Questions: loading...")
    print(f"{'=' * 72}")
    
    # Load data
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        return 1
    
    snapshot = load_snapshot()
    questions = load_questions()
    
    print(f"\n  Snapshot: {snapshot.snapshot_id}")
    print(f"  Entities: {list(snapshot.entities.keys())}")
    print(f"  Measures: {len(snapshot.catalog_index)}")
    print(f"  Questions: {len(questions)}")
    
    # Step 1: Analyze routability
    print(f"\n{'─' * 72}")
    print(f"  Step 1: Routability Analysis (gold SQL vs governed model)")
    print(f"{'─' * 72}")
    
    categories = analyze_gold_sqls(questions, snapshot)
    
    for cat in ["routable", "partial", "not_routable"]:
        items = categories[cat]
        print(f"\n  {cat.upper()} ({len(items)}):")
        for q, reason in items[:8]:
            tags = classify_sql(q["SQL"])
            print(f"    [{q['question_id']}] {q['question'][:60]:<60} {reason[:30]}")
        if len(items) > 8:
            print(f"    ... and {len(items)-8} more")
    
    # Step 2: Mock router benchmark on routable candidates
    routable_qs = [item[0] for item in categories["routable"]]
    partial_qs = [item[0] for item in categories["partial"]]
    
    print(f"\n{'─' * 72}")
    print(f"  Step 2: Mock Router Benchmark (ideal filters from gold SQL)")
    print(f"  Candidates: {len(routable_qs)} routable + {len(partial_qs)} partial = {len(routable_qs)+len(partial_qs)}")
    print(f"{'─' * 72}")
    
    all_candidates = routable_qs + partial_qs
    
    if all_candidates:
        mock_results = mock_router_benchmark(all_candidates, snapshot)
        print_results_table(mock_results, "Mock Router Results")
        
        # Detail: which ones got EX=1.0?
        perfect = [r for r in mock_results if r[1] == "ok" and r[3] == 1.0]
        failed = [r for r in mock_results if r[1] == "ok" and r[3] < 1.0]
        
        if perfect:
            print(f"\n  Perfect matches ({len(perfect)}):")
            for r in perfect:
                print(f"    ✅ [{r[0]['question_id']}] EX={r[3]:.1f} — {r[2][:80]}")
    else:
        print("  No routable candidates found.")
        mock_results = []
    
    # Step 3: Real LLM router benchmark (subset)
    print(f"\n{'─' * 72}")
    print(f"  Step 3: Real LLM Router Benchmark (subset)")
    print(f"  Using OpenRouter gpt-4o-mini")
    print(f"{'─' * 72}")
    
    # Take up to 10 questions: mix of routable + partial
    test_subset = (routable_qs + partial_qs)[:10]
    if test_subset:
        real_results = real_llm_benchmark(test_subset, snapshot)
        print_results_table(real_results, "Real LLM Router Results")
    else:
        print("  No candidates for real LLM benchmark.")
    
    # Summary
    print(f"\n{'=' * 72}")
    print(f"  SUMMARY")
    print(f"{'=' * 72}")
    
    if mock_results:
        mock_ok = sum(1 for r in mock_results if r[1] == "ok")
        mock_ex_scores = [r[3] for r in mock_results if r[1] == "ok" and isinstance(r[3], (int, float))]
        mock_avg_ex = sum(mock_ex_scores) / len(mock_ex_scores) if mock_ex_scores else 0.0
        print(f"  Mock router: {mock_ok}/{len(mock_results)} compiled (avg EX={mock_avg_ex*100:.1f}%)")
    
    overall_compiled = sum(1 for r in all_candidates if find_measure_for_gold(r["SQL"], snapshot)[0] is not None)
    print(f"  Governed model coverage: {overall_compiled}/{len(questions)} questions have a matching measure ({overall_compiled/len(questions)*100:.0f}%)")
    print(f"  Estimated max EX potential: {mock_avg_ex*100:.1f}% on routable questions")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
