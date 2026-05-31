#!/usr/bin/env python3
"""Run full BIRD dev set (1,534 questions) on a single config.

Usage:
  .venv/bin/python scripts/run_full_benchmark.py --config 6   # V4 Pro high few-shot
  .venv/bin/python scripts/run_full_benchmark.py --config 2   # V4 Flash xhigh few-shot
"""
import sys, os, json, time, re, sqlite3, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load API key
env_path = os.path.expanduser("~/.hermes/.env")
for line in open(env_path):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1); k, v = k.strip(), v.strip().strip("'\"")
        if k == "DEEPSEEK_API_KEY": os.environ[k] = v

from src.semantic_registry.pipeline.llm_gateway import DeepSeekProvider
from scripts.bird_schema_context import build_schema_context
from scripts.sql_pattern_memory import SQLPatternMemory, classify_query_type
from src.semantic_registry.registry.db_registry import DBRegistry

CONFIGS = [
    ("V4 Flash · zero_shot · high",     "deepseek-v4-flash", "high",    False),
    ("V4 Flash · few_shot · high",      "deepseek-v4-flash", "high",    True),
    ("V4 Flash · zero_shot · xhigh",    "deepseek-v4-flash", "xhigh",   False),
    ("V4 Flash · few_shot · xhigh",     "deepseek-v4-flash", "xhigh",   True),
    ("V4 Pro · zero_shot · medium",     "deepseek-v4-pro",   "medium",  False),
    ("V4 Pro · few_shot · medium",      "deepseek-v4-pro",   "medium",  True),
    ("V4 Pro · zero_shot · high",       "deepseek-v4-pro",   "high",    False),
    ("V4 Pro · few_shot · high",        "deepseek-v4-pro",   "high",    True),
]

def get_schema_text(db_root, db_id):
    db_path = os.path.join(db_root, db_id, f"{db_id}.sqlite")
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL")
        schemas = [row[0] for row in c.fetchall()]
        conn.close()
        return "\n\n".join(schemas)
    except:
        return ""

def extract_sql_from_response(raw):
    if not raw: return ""
    try:
        start = raw.find("{")
        if start >= 0:
            depth, in_str, quote = 0, False, ""
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
                        cand = raw[start:i+1]
                        cand = re.sub(r",\s*([}\]])", r"\1", cand)
                        d = json.loads(cand)
                        if "sql" in d: return d["sql"]
                        break
    except: pass
    m = re.search(r"SELECT\s+.*?(?:;|$)", raw, re.DOTALL | re.IGNORECASE)
    return m.group(0).strip().rstrip(";") if m else ""

def get_db_schema_map(db_path):
    schema = {}
    try:
        conn = sqlite3.connect(db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        for table in tables:
            cols = conn.execute(f'PRAGMA table_info("{table.replace(chr(34), chr(34)*2)}")').fetchall()
            schema[table.lower()] = {c[1].lower() for c in cols}
        conn.close()
    except Exception:
        return {}
    return schema

def execute_sql(db_path, sql):
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute(sql)
        rows = c.fetchall()
        conn.close()
        return {"ok": True, "rows": rows, "row_count": len(rows), "error": ""}
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return {"ok": False, "rows": [], "row_count": 0, "error": str(e)}

def _ranking_question(question):
    return bool(re.search(r"\b(top|highest|lowest|most|least|largest|smallest|first|last|best|worst|rank|ranking)\b", question or "", re.I))

def _ratio_question(question):
    return bool(re.search(r"\b(rate|ratio|percent|percentage|proportion|average|avg|per\s+cent)\b", question or "", re.I))

def validate_sql(sql, question, db_path):
    issues = []
    upper = sql.upper()
    schema = get_db_schema_map(db_path)

    qualified = re.findall(
        r'(?:(?:"([^"]+)"|`([^`]+)`|([A-Za-z_][\w]*))\s*\.)\s*(?:"([^"]+)"|`([^`]+)`|([A-Za-z_][\w]*))',
        sql,
    )
    for q in qualified:
        table = (q[0] or q[1] or q[2] or "").lower()
        col = (q[3] or q[4] or q[5] or "").lower()
        if schema and table in schema and col not in schema[table]:
            issues.append({"code": "unknown_column", "message": f"Unknown column reference: {table}.{col}"})

    where_match = re.search(r"\bWHERE\b(.*?)(\bGROUP\s+BY\b|\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|$)", sql, re.I | re.S)
    if where_match and re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", where_match.group(1), re.I):
        issues.append({"code": "aggregate_in_where", "message": "Aggregate functions cannot be filtered in WHERE; use HAVING or a subquery."})

    has_order = "ORDER BY" in upper
    has_limit = bool(re.search(r"\bLIMIT\s+\d+", sql, re.I))
    if _ranking_question(question) and not (has_order and has_limit):
        issues.append({"code": "missing_ranking_construct", "message": "Ranking question should usually include both ORDER BY and LIMIT."})
    if _ranking_question(question) and has_limit and not has_order:
        issues.append({"code": "limit_without_order", "message": "LIMIT without ORDER BY is unstable for ranking questions."})
    if _ratio_question(question) and "/" in sql and not re.search(r"\b(CAST|1\.0\s*\*|\*\s*1\.0|100\.0|NULLIF)\b", sql, re.I):
        issues.append({"code": "integer_division", "message": "Ratio/rate division may need CAST, 1.0 multiplication, or NULLIF to avoid integer division/zero division."})

    return {"ok": not issues, "issues": issues}

def should_retry(question, sql, validation_result):
    if not sql:
        return True
    execution = validation_result.get("execution", {})
    if execution and not execution.get("ok"):
        return True
    if execution and execution.get("ok") and execution.get("row_count") == 0:
        if not re.search(r"\b(none|zero|no\s+|without|not any)\b", question or "", re.I):
            return True
    return any(i["code"] in {
        "missing_ranking_construct",
        "limit_without_order",
        "aggregate_in_where",
        "integer_division",
        "unknown_column",
    } for i in validation_result.get("issues", []))

def _repair_reason(question, validation_result):
    execution = validation_result.get("execution", {})
    if execution and not execution.get("ok"):
        return f"SQL execution failed: {execution.get('error')}"
    if execution and execution.get("ok") and execution.get("row_count") == 0:
        return "SQL executed but returned an empty result. Check filter values, joins, and whether sample values/evidence imply a different literal."
    issues = validation_result.get("issues", [])
    if issues:
        return "; ".join(i["message"] for i in issues)
    return "The SQL did not satisfy the validation checks."

def build_repair_prompt(schema, question, evidence, failed_sql, validation_result):
    reason = _repair_reason(question, validation_result)
    parts = [
        "You are repairing a SQLite query. Return ONLY valid JSON.",
        "Original question:",
        question,
    ]
    if evidence:
        parts.extend(["BIRD evidence:", evidence])
    parts.extend([
        "Schema context:",
        schema,
        "Failed SQL:",
        failed_sql or "(no SQL was produced)",
        "Why it failed:",
        reason,
        "Fix only this issue while preserving the requested answer. Generate a single SQLite SELECT statement.",
        'Return ONLY: {"sql": "SELECT ...", "assumptions": [], "tables_used": [], "columns_used": [], "confidence": "high|medium|low", "reasoning_summary": "..."}',
    ])
    return "\n\n".join(parts)

def choose_best(attempts):
    if not attempts:
        return ""
    def score(a):
        execution = a.get("execution", {})
        validation = a.get("validation", {})
        return (
            1 if execution.get("ok") else 0,
            1 if execution.get("row_count", 0) > 0 else 0,
            1 if validation.get("ok") else 0,
            execution.get("row_count", 0),
            -a.get("attempt", 0),
        )
    return max(attempts, key=score).get("sql", "")

def generate_with_repair(provider, schema, question, evidence, db_path, max_repairs=2, base_prompt=None):
    attempts = []
    prompt = base_prompt
    if prompt is None:
        parts = [
            "You are a SQLite expert. Generate a single SELECT statement.",
            "Database Schema:", schema,
        ]
        if evidence:
            parts.append(f"Hint: {evidence}")
        parts.append(f"Question: {question}")
        parts.append('Return ONLY: {"sql": "SELECT ...", "assumptions": [], "tables_used": [], "columns_used": [], "confidence": "high|medium|low", "reasoning_summary": "..."}')
        prompt = "\n\n".join(parts)

    for attempt_idx in range(max_repairs + 1):
        raw = provider.generate(f"Return ONLY valid JSON.\n\n{prompt}")
        sql = extract_sql_from_response(raw)
        validation = validate_sql(sql, question, db_path) if sql else {"ok": False, "issues": [{"code": "no_sql", "message": "No SELECT SQL was produced."}]}
        execution = execute_sql(db_path, sql) if sql else {"ok": False, "rows": [], "row_count": 0, "error": "No SQL produced"}
        validation["execution"] = execution
        attempts.append({
            "attempt": attempt_idx + 1,
            "sql": sql,
            "validation": {"ok": validation["ok"], "issues": validation["issues"]},
            "execution": {"ok": execution["ok"], "row_count": execution["row_count"], "error": execution["error"]},
            "repair_reason": "" if attempt_idx == 0 else _repair_reason(question, validation),
        })
        if attempt_idx >= max_repairs or not should_retry(question, sql, validation):
            break
        prompt = build_repair_prompt(schema, question, evidence, sql, validation)

    return choose_best(attempts), attempts

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=int, required=True, help="Config index 0-7")
    parser.add_argument("--resume", type=int, default=0, help="Resume from question index")
    parser.add_argument("--sample", type=int, default=0, help="Run only the first N questions")
    parser.add_argument("--indices", type=str, default=None, help="Path to sample indices JSON file")
    parser.add_argument("--memory", choices=["v1", "v25"], default="v1", help="Pattern memory implementation")
    args = parser.parse_args()

    config_idx = args.config
    name, model, reasoning, few_shot = CONFIGS[config_idx]

    # Paths
    base = os.path.dirname(os.path.abspath(__file__))
    bird_dir = os.path.join(base, "..", "bird_bench")
    dev_path = os.path.join(bird_dir, "dev", "dev_20240627", "dev.json")
    db_root = os.path.join(bird_dir, "dev", "dev_20240627", "databases", "dev_databases")
    results_dir = os.path.join(bird_dir, "results", "full_benchmarks")
    os.makedirs(results_dir, exist_ok=True)

    # Load ALL questions
    with open(dev_path) as f: dev = json.load(f)
    if args.indices:
        with open(args.indices) as f:
            indices = json.load(f)
        dev = [dev[i] for i in indices]
        print(f"Using sample indices: {len(dev)} questions from {args.indices}")
    elif args.sample > 0:
        dev = dev[:args.sample]
    total = len(dev)
    print(f"Full dev set: {total} questions")

    # Seed pattern memory
    if args.memory == "v25":
        from scripts.pattern_memory_v25 import PatternMemoryV25

        registry = DBRegistry()
        v25_memory_dir = tempfile.TemporaryDirectory(prefix="nl2sql_v25_bird_seed_")
        v25_memory_path = os.path.join(v25_memory_dir.name, "patterns_v25.db")
        memory = PatternMemoryV25(db_path=v25_memory_path, registry=registry)
        print("Seeding V2.5 pattern memory from BIRD...")
        counts = memory.seed_from_bird(dev_path=dev_path, db_root=db_root)
        print(f"  Added: {counts['added']}, Skipped: {counts['skipped']}, Registered DBs: {counts['registered']}")
        print(f"Pattern memory v25 has {memory.store.count()} seed patterns")
    else:
        memory = SQLPatternMemory()
        if memory.store.count() < 100:
            print("Seeding pattern memory from BIRD...")
            memory.seed_from_bird(dev_path)
        print(f"Pattern memory has {memory.store.count()} patterns")

    provider = DeepSeekProvider(model=model, reasoning_effort=reasoning)

    # Progress tracking
    safe_name = name.replace("·", "").replace(" ", "_").replace("__", "_")
    if args.indices:
        indices_name = os.path.splitext(os.path.basename(args.indices))[0]
        safe_name = f"{safe_name}_{indices_name}"
    elif args.sample > 0:
        safe_name = f"{safe_name}_sample{args.sample}"
    if args.memory != "v1":
        safe_name = f"{safe_name}_{args.memory}"
    progress_path = os.path.join(results_dir, f"full_{safe_name}_progress.json")
    results_path = os.path.join(results_dir, f"full_{safe_name}.json")

    if args.resume > 0 and os.path.exists(progress_path):
        with open(progress_path) as f:
            existing = json.load(f)
        results = existing["results"]
        start_idx = len(results)
        print(f"Resuming from question {start_idx}/{total} ({existing['passed']} correct so far)")
    else:
        results = []
        start_idx = 0

    start_time = time.time()

    for i in range(start_idx, total):
        q = dev[i]
        db_id = q["db_id"]
        question = q["question"]
        gold_sql = q["SQL"]
        evidence = q.get("evidence", "")

        db_path = os.path.join(db_root, db_id, f"{db_id}.sqlite")
        if args.memory == "v25":
            memory.ensure_database(db_id, db_path=db_path)
        schema = build_schema_context(db_root, db_id, question, evidence)
        if i == start_idx:
            print("\nEnriched schema preview:")
            print(schema[:2000] + ("..." if len(schema) > 2000 else ""))
            print()

        if few_shot:
            if args.memory == "v25":
                patterns = memory.retrieve(question, db_id, top_k=3)
            else:
                qtype = classify_query_type(gold_sql)
                patterns = memory.retrieve(question, db_id, qtype, top_k=3)
            prompt = memory.build_prompt(question, db_id, schema, patterns, evidence)
        else:
            parts = [
                "You are a SQLite expert. Generate a single SELECT statement.",
                "Database Schema:", schema,
            ]
            if evidence: parts.append(f"Hint: {evidence}")
            parts.append(f"Question: {question}")
            parts.append('Return ONLY: {"sql": "SELECT ...", "assumptions": [], "tables_used": [], "columns_used": [], "confidence": "high|medium|low", "reasoning_summary": "..."}')
            prompt = "\n\n".join(parts)

        attempts = []
        try:
            sql, attempts = generate_with_repair(provider, schema, question, evidence, db_path, max_repairs=2, base_prompt=prompt)
        except Exception as e:
            sql = ""
            attempts = []
            print(f"  ERROR: {e}")

        match = False
        if sql:
            try:
                conn = sqlite3.connect(db_path); c = conn.cursor()
                c.execute(sql); pred = c.fetchall()
                c.execute(gold_sql); gold = c.fetchall()
                conn.close()
                match = set(pred) == set(gold)
            except Exception as e:
                print(f"  SQL ERROR: {e}")

        if match:
            if args.memory == "v25":
                # Keep benchmark comparisons immutable: V2.5 is seeded from BIRD only.
                pass
            else:
                memory.record_match(question, sql, db_id)

        repair_used = len(attempts) > 1
        results.append({
            "idx": i,
            "db_id": db_id,
            "match": match,
            "sql": sql,
            "attempts": len(attempts),
            "repair_used": repair_used,
            "repair_attempts": attempts,
        })
        passed = sum(1 for r in results if r["match"])

        # Print progress
        elapsed = time.time() - start_time
        rate = (i + 1) / elapsed * 60 if elapsed > 0 else 0
        print(f"  [{i+1}/{total}] {'✅' if match else '❌'} {db_id:25} repairs={max(0, len(attempts)-1)} pass={passed}/{i+1} ({passed/(i+1)*100:.1f}%) ({rate:.0f}/min)", flush=True)

        # Save progress every 50 questions
        if (i + 1) % 50 == 0 or i == total - 1:
            with open(progress_path, "w") as f:
                json.dump({"results": results, "passed": passed, "last_idx": i}, f)
            # Also save complete results periodically
            report = {
                "config": {"name": name, "model": model, "reasoning": reasoning, "few_shot": few_shot},
                "memory": args.memory,
                "total": i + 1,
                "passed": passed,
                "ex": round(passed / (i + 1) * 100, 2),
                "time_min": round(elapsed / 60, 1),
                "results": results,
            }
            with open(results_path, "w") as f:
                json.dump(report, f, indent=2)

    # Final save
    passed = sum(1 for r in results if r["match"])
    ex = passed / total * 100
    report = {
        "config": {"name": name, "model": model, "reasoning": reasoning, "few_shot": few_shot},
        "memory": args.memory,
        "total": total,
        "passed": passed,
        "ex": round(ex, 2),
        "time_min": round((time.time() - start_time) / 60, 1),
        "results": results,
    }
    with open(results_path, "w") as f:
        json.dump(report, f, indent=2)
    # Clean up progress file
    if os.path.exists(progress_path):
        os.remove(progress_path)

    print(f"\n{'='*60}")
    print(f"  FINISHED: {name}")
    print(f"  EX: {ex:.1f}% ({passed}/{total}) in {report['time_min']}min")
    print(f"  Results: {results_path}")

if __name__ == "__main__":
    main()
