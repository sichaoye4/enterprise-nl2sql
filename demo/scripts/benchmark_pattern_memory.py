#!/usr/bin/env python3
"""
Benchmark SQL Pattern Memory with different model/reasoning/few-shot configs.

Usage:
  .venv/bin/python scripts/benchmark_pattern_memory.py --quick      # 30 questions, all configs
  .venv/bin/python scripts/benchmark_pattern_memory.py --full       # All 110, all configs
  .venv/bin/python scripts/benchmark_pattern_memory.py --sample 50  # Custom sample
"""

import sys, os, json, time, re, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load API key
env_path = os.path.expanduser("~/.hermes/.env")
for line in open(env_path):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1); k, v = k.strip(), v.strip().strip("'\"")
        if k == "DEEPSEEK_API_KEY": os.environ[k] = v

from src.semantic_registry.pipeline.llm_gateway import DeepSeekProvider
from scripts.sql_pattern_memory import SQLPatternMemory, classify_query_type


# ── Configuration Matrix ────────────────────────────────────────────────────

CONFIGS = [
    # (name, model, reasoning_effort, use_few_shot)
    ("V4 Flash · zero_shot · high",     "deepseek-v4-flash", "high",    False),
    ("V4 Flash · few_shot · high",      "deepseek-v4-flash", "high",    True),
    ("V4 Flash · zero_shot · xhigh",    "deepseek-v4-flash", "xhigh",   False),
    ("V4 Flash · few_shot · xhigh",     "deepseek-v4-flash", "xhigh",   True),
    ("V4 Pro · zero_shot · medium",     "deepseek-v4-pro",   "medium",  False),
    ("V4 Pro · few_shot · medium",      "deepseek-v4-pro",   "medium",  True),
    ("V4 Pro · zero_shot · high",       "deepseek-v4-pro",   "high",    False),
    ("V4 Pro · few_shot · high",        "deepseek-v4-pro",   "high",    True),
]


def get_schema_text(db_root: str, db_id: str) -> str:
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


def extract_sql_from_response(raw: str) -> str:
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


def evaluate_config(name: str, model: str, reasoning: str, use_few_shot: bool,
                    questions: list, memory: SQLPatternMemory, db_root: str,
                    results_dir: str) -> dict:
    """Run evaluation for one config."""
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"  Model: {model}, Reasoning: {reasoning}, Few-shot: {use_few_shot}")
    print(f"{'='*60}")
    
    provider = DeepSeekProvider(model=model, reasoning_effort=reasoning)
    
    results = []
    start = time.time()
    
    for i, (idx, q) in enumerate(questions):
        db_id = q["db_id"]
        question = q["question"]
        gold_sql = q["SQL"]
        evidence = q.get("evidence", "")
        schema = get_schema_text(db_root, db_id)
        
        if use_few_shot:
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
        
        try:
            raw = provider.generate(f"Return ONLY valid JSON.\n\n{prompt}")
            sql = extract_sql_from_response(raw)
        except Exception as e:
            sql = ""
        
        match = False
        if sql:
            try:
                db_path = os.path.join(db_root, db_id, f"{db_id}.sqlite")
                conn = sqlite3.connect(db_path); c = conn.cursor()
                c.execute(sql); pred = c.fetchall()
                c.execute(gold_sql); gold = c.fetchall()
                conn.close()
                match = set(pred) == set(gold)
            except: pass
        
        if match:
            memory.record_match(question, sql, db_id)
        
        results.append({"idx": idx, "match": match})
        
        elapsed = time.time() - start
        rate = (i + 1) / elapsed * 60 if elapsed > 0 else 0
        print(f"  [{i+1}/{len(questions)}] {'✅' if match else '❌'} ({rate:.0f}/min)", flush=True)
    
    passed = sum(1 for r in results if r["match"])
    ex = passed / len(results) * 100
    
    # Save result
    safe_name = name.replace("·", "").replace(" ", "_").replace("__", "_")
    report = {
        "config": {"name": name, "model": model, "reasoning": reasoning, "few_shot": use_few_shot},
        "total": len(results),
        "passed": passed,
        "ex": round(ex, 2),
        "time_min": round((time.time() - start) / 60, 1),
    }
    path = os.path.join(results_dir, f"benchmark_{safe_name}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    
    print(f"  EX: {ex:.1f}% ({passed}/{len(results)}) in {report['time_min']}min")
    return report


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="30 questions, all configs")
    parser.add_argument("--full", action="store_true", help="110 questions, all configs")
    parser.add_argument("--sample", type=int, default=None, help="Custom sample size")
    parser.add_argument("--configs", type=str, default=None, help="Comma-separated config indices")
    args = parser.parse_args()
    
    # Determine sample size
    if args.quick: n = 30
    elif args.full: n = 110
    elif args.sample: n = args.sample
    else: n = 30  # Default to quick
    
    # Paths
    base = os.path.dirname(os.path.abspath(__file__))
    bird_dir = os.path.join(base, "..", "bird_bench")
    dev_path = os.path.join(bird_dir, "dev", "dev_20240627", "dev.json")
    db_root = os.path.join(bird_dir, "dev", "dev_20240627", "databases", "dev_databases")
    results_dir = os.path.join(bird_dir, "results", "benchmarks")
    os.makedirs(results_dir, exist_ok=True)
    
    # Load questions
    with open(dev_path) as f: dev = json.load(f)
    with open(os.path.join(base, "..", "bird_bench", "results", "sample_indices.json")) as f:
        indices = json.load(f)
    questions = [(idx, dev[idx]) for idx in indices[:n]]
    
    # Seed pattern memory
    memory = SQLPatternMemory()
    if memory.store.count() < 100:
        print("Seeding pattern memory...")
        memory.seed_from_bird(dev_path)
    
    print(f"\nBenchmark: {len(questions)} questions across 11 databases")
    print(f"Testing {len(CONFIGS)} configurations")
    
    # Filter configs if specified
    configs = CONFIGS
    if args.configs:
        idxs = [int(i.strip()) for i in args.configs.split(",")]
        configs = [CONFIGS[i] for i in idxs if i < len(CONFIGS)]
    
    all_results = []
    for name, model, reasoning, few_shot in configs:
        r = evaluate_config(name, model, reasoning, few_shot, questions, memory, db_root, results_dir)
        all_results.append(r)
    
    # Summary
    print(f"\n{'='*60}")
    print(f"  BENCHMARK SUMMARY")
    print(f"{'='*60}")
    print(f"{'Configuration':50} {'EX%':>8} {'Time':>8}")
    print("-" * 68)
    for r in sorted(all_results, key=lambda x: -x["ex"]):
        cfg = r["config"]
        name = f"{cfg['model']} · {cfg['reasoning']} · {'few' if cfg['few_shot'] else 'zero'}"
        print(f"  {name:48} {r['ex']:>7.1f}% {r['time_min']:>7.1f}min")
    
    # Save summary
    summary_path = os.path.join(results_dir, "_summary.json")
    with open(summary_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSummary saved: {summary_path}")


if __name__ == "__main__":
    main()
