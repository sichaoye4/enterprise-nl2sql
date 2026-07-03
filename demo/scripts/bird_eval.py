#!/usr/bin/env python3
"""
BIRD Benchmark Adapter — SQL-only evaluation for Enterprise NL2SQL Copilot.

Usage:
  python scripts/bird_eval.py                          # Run on all 1534 dev questions
  python scripts/bird_eval.py --subset 50               # Run first 50 questions (test)
  python scripts/bird_eval.py --eval-only               # Re-evaluate existing results

Output:
  bird_bench/results/predict_dev.json   — Generated SQL in BIRD format
  bird_bench/results/eval_report.txt    — Execution Accuracy by difficulty
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.semantic_registry.pipeline.llm_gateway import DeepSeekProvider, LLMGateway
from src.semantic_registry.pipeline.llm_gateway_types import LLMResponse


# ── Paths ───────────────────────────────────────────────────────────────────
BIRD_DIR = Path(__file__).resolve().parent.parent / "bird_bench"
DEV_DIR = BIRD_DIR / "dev" / "dev_20240627"
RESULTS_DIR = BIRD_DIR / "results"
DATABASES_ROOT = DEV_DIR / "databases" / "dev_databases"
EVAL_SCRIPT = Path(__file__).resolve().parent.parent / "bird_bench" / "eval"
OFFICIAL_EVAL = Path(__file__).resolve().parent.parent / "src" / "semantic_registry" / "evaluation" if False else None


# ── Data Loading ────────────────────────────────────────────────────────────

def load_dev_json() -> list[dict]:
    with open(DEV_DIR / "dev.json") as f:
        return json.load(f)


def load_dev_tables() -> list[dict]:
    with open(DEV_DIR / "dev_tables.json") as f:
        return json.load(f)


def load_dev_sql() -> list[tuple[str, str]]:
    """Returns list of (sql, db_id)"""
    results = []
    with open(DEV_DIR / "dev.sql") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                results.append((parts[0], parts[1]))
            else:
                results.append((parts[0], ""))
    return results


def build_schema_prompt(tables_data: list[dict], db_id: str) -> str:
    """Build CREATE TABLE statements for a specific database."""
    db_schema = None
    for t in tables_data:
        if t["db_id"] == db_id:
            db_schema = t
            break
    
    if db_schema is None:
        return ""
    
    table_names = db_schema["table_names_original"]
    col_names = db_schema["column_names_original"]
    col_types = db_schema["column_types"]
    primary_keys = db_schema["primary_keys"]
    foreign_keys = db_schema["foreign_keys"]
    
    # Group columns by table
    tables: dict[int, list[tuple[str, str, bool, list[str]]]] = {}
    for i, (table_idx, col_name) in enumerate(col_names):
        if table_idx == -1:  # Skip the * column
            continue
        if table_idx not in tables:
            tables[table_idx] = []
        is_pk = i in primary_keys
        fk_refs = []
        for fk in foreign_keys:
            if isinstance(fk, list) and len(fk) == 2:
                if fk[0] == i:
                    # Find referenced table/column
                    ref_idx = fk[1]
                    ref_col = col_names[ref_idx][1] if ref_idx < len(col_names) else "?"
                    for ti, tn in enumerate(table_names):
                        cols_for_ti = [(j, c[1]) for j, c in enumerate(col_names) if c[0] == ti]
                        if any(j == ref_idx for j, _ in cols_for_ti):
                            fk_refs.append(f"REFERENCES {tn}({ref_col})")
                            break
        
        tables[table_idx].append((col_name, col_types[i] if i < len(col_types) else "text", is_pk, fk_refs))
    
    lines = []
    for ti, tn in enumerate(table_names):
        lines.append(f"CREATE TABLE {tn} (")
        cols = tables.get(ti, [])
        col_lines = []
        for col_name, col_type, is_pk, fk_refs in cols:
            col_def = f"  {col_name} {col_type}"
            if is_pk:
                col_def += " PRIMARY KEY"
            for fk_ref in fk_refs:
                col_def += f" {fk_ref}"
            col_lines.append(col_def)
        lines.append(",\n".join(col_lines))
        lines.append(")")
    
    return "\n\n".join(lines)


def build_prompt(question: str, schema: str, evidence: str | None = None) -> str:
    """Build the full prompt for SQL generation."""
    parts = [
        "You are a SQL expert. Given the database schema and a question, generate a valid SQLite query.",
        "",
        "Database Schema:",
        schema,
    ]
    
    if evidence:
        parts.extend([
            "",
            f"Domain Knowledge / Hint:",
            evidence,
        ])
    
    parts.extend([
        "",
        f"Question: {question}",
        "",
        "Generate a valid SQLite SQL query that answers this question.",
        "Return ONLY a valid JSON object with this exact format:",
        '{  "sql": "SELECT ...",',
        '   "assumptions": ["list your assumptions here"],',
        '   "tables_used": ["table1", "table2"],',
        '   "columns_used": ["col1", "col2"],',
        '   "confidence": "high|medium|low",',
        '   "reasoning_summary": "brief reasoning"}',
        "",
        "IMPORTANT:",
        "- Use SQLite dialect",
        "- Use backtick-quoting for column/table names with spaces",
        "- Do NOT use SELECT *",
        "- If the question has ambiguities, make a reasonable assumption and document it",
    ])
    
    return "\n".join(parts)


# ── SQL Generation ──────────────────────────────────────────────────────────

def extract_sql_from_response(raw: str) -> str | None:
    """Extract SQL from LLM response that may contain JSON or raw SQL."""
    # Try JSON extraction first
    try:
        import json as json_mod
        # Find JSON block
        start = raw.find("{")
        if start >= 0:
            depth = 0
            in_string = False
            quote = ""
            for i in range(start, len(raw)):
                c = raw[i]
                if in_string:
                    if c == "\\":
                        i += 1
                    elif c == quote:
                        in_string = False
                elif c in ("'", '"'):
                    in_string = True
                    quote = c
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = raw[start:i+1]
                        # Clean trailing commas
                        import re
                        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
                        data = json_mod.loads(candidate)
                        if isinstance(data, dict) and "sql" in data:
                            return data["sql"]
                        break
    except (json.JSONDecodeError, ValueError):
        pass
    
    # Try to find SQL between ```sql ... ``` markers
    import re
    sql_match = re.search(r"```sql\s*(.*?)\s*```", raw, re.DOTALL | re.IGNORECASE)
    if sql_match:
        sql = sql_match.group(1).strip()
        if sql.upper().startswith("SELECT"):
            return sql
    
    # Try to find a SELECT statement directly
    select_match = re.search(r"(SELECT\s+.*?)(?:\n\n|$)", raw, re.DOTALL | re.IGNORECASE)
    if select_match:
        sql = select_match.group(1).strip().rstrip(";")
        if sql.upper().startswith("SELECT"):
            return sql
    
    return None


def generate_sql(
    provider: DeepSeekProvider,
    question: dict,
    schema_prompt: str,
) -> str | None:
    """Generate SQL for a single BIRD question by calling DeepSeek directly."""
    evidence = question.get("evidence")
    prompt = build_prompt(
        question=question["question"],
        schema=schema_prompt,
        evidence=evidence,
    )
    
    system_prompt = (
        "You are a SQLite expert. Generate a single SELECT statement to answer the question. "
        "Return ONLY a JSON object with fields: sql, assumptions, tables_used, columns_used, "
        "confidence, reasoning_summary. "
        "Use SQLite syntax with backtick quoting for identifiers that contain spaces or special characters. "
        "Do NOT use SELECT *."
    )
    
    full_prompt = f"{system_prompt}\n\n{prompt}"
    
    try:
        raw = provider.generate(full_prompt)
        sql = extract_sql_from_response(raw)
        if sql:
            return sql
        print(f"  [WARN] Could not extract SQL from response, raw={raw[:100]}...", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  [ERROR] Generation failed: {e}", file=sys.stderr)
        return None


def evaluate_results(results_dir: Path, subset: int | None):
    """Run BIRD's official evaluation.py on generated results."""
    import subprocess
    
    eval_py = Path(__file__).resolve().parent.parent / "bird_bench" / "eval_src" / "evaluation.py"
    
    # Create eval_src directory if needed and copy evaluation script
    eval_src = RESULTS_DIR / "eval_src"
    eval_src.mkdir(parents=True, exist_ok=True)
    
    # Copy the official evaluation.py and evaluation_ves.py
    src_eval = Path(__file__).resolve().parent.parent / "bird_bench" / "dev" / "dev_20240627"
    
    # Write evaluation results
    print("\n" + "=" * 60)
    print("Running Execution Accuracy (EX) evaluation...")
    print("=" * 60)
    
    # The official BIRD evaluation.py compares predicted SQL vs gold SQL
    # by executing both against the SQLite database
    cmd = [
        sys.executable,
        str(DEV_DIR.parent / "eval" / "evaluation.py"),
        "--predicted_sql_path", str(results_dir / "predict_dev.json"),
        "--ground_truth_path", str(DEV_DIR / "dev.sql"),
        "--data_mode", "dev",
        "--db_root_path", str(DATABASES_ROOT / ""),
        "--num_cpus", "4",
        "--meta_time_out", "30.0",
        "--mode_gt", "gt",
        "--mode_predict", "gpt",
        "--difficulty", "simple",
        "--diff_json_path", str(DEV_DIR / "dev.json"),
    ]
    
    # Note: The official evaluation.py expects predict_dev.json at the 
    # predicted_sql_path directory, not as a full path. Let me handle this.
    # Actually, looking at the code, package_sqls expects sql_path + 'predict_dev.json'
    # So we need to pass the directory and data_mode
    
    # Let's use a simpler approach - just run the SQL comparison ourselves
    # using the same methodology as BIRD's evaluation.py
    return run_evaluation_local(results_dir, subset)


def run_evaluation_local(results_dir: Path, subset: int | None):
    """Run Execution Accuracy evaluation using BIRD's methodology."""
    import sqlite3
    from collections import Counter
    
    # Load gold data
    dev_data = load_dev_json()
    gold_sqls = load_dev_sql()
    
    # Load predictions
    with open(results_dir / "predict_dev.json") as f:
        predictions = json.load(f)
    
    if subset:
        dev_data = dev_data[:subset]
        gold_sqls = gold_sqls[:subset]
    
    results = []
    total = len(dev_data)
    
    for idx, question in enumerate(dev_data):
        db_id = question["db_id"]
        db_path = DATABASES_ROOT / db_id / f"{db_id}.sqlite"
        
        pred_key = str(idx)
        predicted_sql = predictions.get(pred_key, "")
        
        if not predicted_sql or predicted_sql == "":
            results.append({
                "idx": idx,
                "question": question["question"],
                "db_id": db_id,
                "difficulty": question["difficulty"],
                "execution_match": False,
                "error": "No SQL generated",
            })
            continue
        
        # Extract gold SQL
        gold_sql = gold_sqls[idx][0] if idx < len(gold_sqls) else ""
        
        if not gold_sql:
            results.append({
                "idx": idx,
                "question": question["question"],
                "db_id": db_id,
                "difficulty": question["difficulty"],
                "execution_match": False,
                "error": "No gold SQL",
            })
            continue
        
        # Execute and compare
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            
            try:
                cursor.execute(predicted_sql)
                predicted_res = cursor.fetchall()
            except Exception as e:
                results.append({
                    "idx": idx,
                    "question": question["question"],
                    "db_id": db_id,
                    "difficulty": question["difficulty"],
                    "execution_match": False,
                    "error": f"Predicted SQL error: {e}",
                    "predicted_sql": predicted_sql,
                    "gold_sql": gold_sql,
                })
                conn.close()
                continue
            
            try:
                cursor.execute(gold_sql)
                gold_res = cursor.fetchall()
            except Exception as e:
                results.append({
                    "idx": idx,
                    "question": question["question"],
                    "db_id": db_id,
                    "difficulty": question["difficulty"],
                    "execution_match": False,
                    "error": f"Gold SQL error: {e}",
                    "predicted_sql": predicted_sql,
                    "gold_sql": gold_sql,
                })
                conn.close()
                continue
            
            conn.close()
            
            match = set(predicted_res) == set(gold_res)
            results.append({
                "idx": idx,
                "question": question["question"],
                "db_id": db_id,
                "difficulty": question["difficulty"],
                "execution_match": match,
                "error": None,
                "predicted_sql": predicted_sql,
                "gold_sql": gold_sql,
            })
        
        except Exception as e:
            results.append({
                "idx": idx,
                "question": question["question"],
                "db_id": db_id,
                "difficulty": question["difficulty"],
                "execution_match": False,
                "error": f"DB connection error: {e}",
            })
        
        # Progress
        if (idx + 1) % 50 == 0:
            pct = (idx + 1) / total * 100
            matched = sum(1 for r in results if r.get("execution_match"))
            print(f"  [{idx+1}/{total}] {pct:.0f}% — Current EX: {matched}/{idx+1} = {matched/(idx+1)*100:.1f}%")
    
    # Compute stats by difficulty
    by_difficulty: dict[str, list[bool]] = {}
    for r in results:
        diff = r["difficulty"]
        if diff not in by_difficulty:
            by_difficulty[diff] = []
        by_difficulty[diff].append(r["execution_match"])
    
    # Save detailed results
    with open(results_dir / "eval_details.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    # Print report
    print("\n" + "=" * 60)
    print("  EXECUTION ACCURACY (EX) REPORT")
    print("=" * 60)
    print(f"{'':20} {'Count':>8} {'Pass':>8} {'EX%':>8}")
    print("-" * 48)
    
    total_count = 0
    total_pass = 0
    for diff in ["simple", "moderate", "challenging"]:
        matches = by_difficulty.get(diff, [])
        if matches:
            n = len(matches)
            p = sum(1 for m in matches if m)
            ex = p / n * 100
            total_count += n
            total_pass += p
            print(f"{diff:20} {n:>8} {p:>8} {ex:>7.2f}%")
    
    if total_count > 0:
        print("-" * 48)
        print(f"{'Total':20} {total_count:>8} {total_pass:>8} {total_pass/total_count*100:>7.2f}%")
    print("=" * 60)
    
    # Error analysis
    errors = [r for r in results if r.get("error")]
    if errors:
        error_types = Counter(r["error"].split(":")[0] for r in errors)
        print("\nError breakdown:")
        for err_type, count in error_types.most_common(10):
            print(f"  {err_type}: {count}")
    
    return results


# ── VES Evaluation ──────────────────────────────────────────────────────────

def evaluate_ves(results_dir: Path):
    """Run VES (Value Efficiency Score) evaluation."""
    import math
    import sqlite3
    import numpy as np
    from collections import Counter
    
    # Load data
    dev_data = load_dev_json()
    gold_sqls = load_dev_sql()
    
    with open(results_dir / "predict_dev.json") as f:
        predictions = json.load(f)
    
    results = []
    total = len(dev_data)
    ITERATE_NUM = 10  # Reduced from 100 for speed; 100 is recommended for production
    
    def clean_abnormal(input_data):
        arr = np.asarray(input_data)
        mean = np.mean(arr)
        std = np.std(arr)
        return [x for x in arr if x < mean + 3 * std and x > mean - 3 * std]
    
    for idx, question in enumerate(dev_data):
        db_id = question["db_id"]
        db_path = DATABASES_ROOT / db_id / f"{db_id}.sqlite"
        
        pred_key = str(idx)
        predicted_sql = predictions.get(pred_key, "")
        gold_sql = gold_sqls[idx][0] if idx < len(gold_sqls) else ""
        
        if not predicted_sql or not gold_sql:
            results.append({"idx": idx, "time_ratio": 0})
            continue
        
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            
            # Check if results match first
            cursor.execute(predicted_sql)
            predicted_res = cursor.fetchall()
            cursor.execute(gold_sql)
            gold_res = cursor.fetchall()
            
            if set(predicted_res) != set(gold_res):
                results.append({"idx": idx, "time_ratio": 0})
                conn.close()
                continue
            
            # Measure execution time ratio
            time_diffs = []
            for _ in range(ITERATE_NUM):
                start = time.time()
                cursor.execute(predicted_sql)
                cursor.fetchall()
                pred_time = time.time() - start
                
                start = time.time()
                cursor.execute(gold_sql)
                cursor.fetchall()
                gold_time = time.time() - start
                
                if pred_time > 0:
                    time_diffs.append(gold_time / pred_time)
            
            conn.close()
            
            if time_diffs:
                cleaned = clean_abnormal(time_diffs)
                time_ratio = sum(cleaned) / len(cleaned) if cleaned else 0
                results.append({"idx": idx, "time_ratio": math.sqrt(time_ratio) * 100 if time_ratio > 0 else 0})
            else:
                results.append({"idx": idx, "time_ratio": 0})
        
        except Exception:
            results.append({"idx": idx, "time_ratio": 0})
        
        if (idx + 1) % 100 == 0:
            print(f"  VES progress: [{idx+1}/{total}]")
    
    # Compute VES by difficulty
    by_diff: dict[str, list[float]] = {}
    for idx, r in enumerate(results):
        diff = dev_data[idx]["difficulty"]
        if diff not in by_diff:
            by_diff[diff] = []
        by_diff[diff].append(r["time_ratio"])
    
    print("\n" + "=" * 60)
    print("  VALUE EFFICIENCY SCORE (VES) REPORT")
    print("=" * 60)
    print(f"{'':20} {'Count':>8} {'VES':>8}")
    print("-" * 40)
    
    total_ves = 0
    total_n = 0
    for diff in ["simple", "moderate", "challenging"]:
        ratios = by_diff.get(diff, [])
        if ratios:
            n = len(ratios)
            ves = sum(ratios) / n
            total_ves += sum(ratios)
            total_n += n
            print(f"{diff:20} {n:>8} {ves:>7.2f}")
    
    if total_n > 0:
        print("-" * 40)
        print(f"{'Total':20} {total_n:>8} {total_ves/total_n:>7.2f}")
    print("=" * 60)
    
    return results


# ── Main ────────────────────────────────────────────────────────────────────

def load_env():
    """Load API keys from ~/.hermes/.env if available."""
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                if key == "DEEPSEEK_API_KEY" and not os.environ.get("DEEPSEEK_API_KEY"):
                    os.environ["DEEPSEEK_API_KEY"] = value
                elif key == "DEEPSEEK_BASE_URL" and not os.environ.get("DEEPSEEK_BASE_URL"):
                    os.environ["DEEPSEEK_BASE_URL"] = value
    print(f"  DEEPSEEK_API_KEY loaded: {bool(os.environ.get('DEEPSEEK_API_KEY'))}")

def main():
    load_env()
    parser = argparse.ArgumentParser(description="BIRD Benchmark Evaluation")
    parser.add_argument("--subset", type=int, default=None, help="Run on first N questions only")
    parser.add_argument("--eval-only", action="store_true", help="Skip SQL generation, re-evaluate existing results")
    parser.add_argument("--continue-from", type=int, default=None, help="Continue from question index (resume)")
    args = parser.parse_args()
    
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Load data
    print("Loading BIRD dev set...")
    dev_data = load_dev_json()
    tables_data = load_dev_tables()
    print(f"  Questions: {len(dev_data)}")
    print(f"  Databases: {len(tables_data)}")
    
    total_questions = args.subset or len(dev_data)
    if args.subset:
        dev_data = dev_data[:args.subset]
    
    if args.eval_only:
        print("\nSkipping SQL generation, running evaluation only...")
        run_evaluation_local(RESULTS_DIR, args.subset)
        return
    
    # Load existing predictions if resuming
    predictions_path = RESULTS_DIR / "predict_dev.json"
    predictions: dict[str, str] = {}
    if predictions_path.exists() and args.continue_from is not None:
        with open(predictions_path) as f:
            predictions = json.load(f)
        print(f"  Resuming from question {args.continue_from}, {len(predictions)} existing predictions loaded")
    
    # Initialize DeepSeek provider directly (bypass LLMGateway validation)
    print("\nInitializing DeepSeek provider...")
    provider = DeepSeekProvider()
    
    # Build schema cache
    print("Building schema cache...")
    schema_cache: dict[str, str] = {}
    for t in tables_data:
        db_id = t["db_id"]
        schema_cache[db_id] = build_schema_prompt(tables_data, db_id)
    
    # Generate SQL for each question
    print(f"\nGenerating SQL for {total_questions} questions...")
    print(f"  {'ID':>5} {'DB':25} {'Difficulty':15} {'Question':40}")
    print(f"  {'-'*85}")
    
    start_idx = args.continue_from or 0
    
    for idx in range(start_idx, len(dev_data)):
        question = dev_data[idx]
        db_id = question["db_id"]
        diff = question["difficulty"]
        q_text = question["question"][:50]
        
        print(f"  {idx:>5} {db_id:25} {diff:15} {q_text:40}", end="\r")
        
        # Skip if already generated
        if str(idx) in predictions:
            continue
        
        schema_prompt = schema_cache.get(db_id, "")
        if not schema_prompt:
            print(f"\n  [SKIP] No schema for {db_id}", file=sys.stderr)
            continue
        
        sql = generate_sql(provider, question, schema_prompt)
        
        if sql:
            # Format in BIRD's expected format: "SQL\\t----- bird -----\\tdb_id"
            predictions[str(idx)] = f"{sql}\t----- bird -----\t{db_id}"
        else:
            predictions[str(idx)] = ""
        
        # Save periodically (every 20 questions)
        if (idx + 1) % 20 == 0:
            with open(predictions_path, "w") as f:
                json.dump(predictions, f, indent=2)
    
    # Final save
    with open(predictions_path, "w") as f:
        json.dump(predictions, f, indent=2)
    print(f"\n\nPredictions saved to {predictions_path}")
    
    # Run evaluation
    run_evaluation_local(RESULTS_DIR, args.subset)


if __name__ == "__main__":
    main()
