#!/usr/bin/env python3
"""
Run BIRD evaluation on a stratified sample across all databases.
Usage: .venv/bin/python scripts/bird_eval_stratified.py
"""

import json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.semantic_registry.pipeline.llm_gateway import DeepSeekProvider

# Paths
BIRD_DIR = Path(__file__).resolve().parent.parent / "bird_bench"
DEV_DIR = BIRD_DIR / "dev" / "dev_20240627"
RESULTS_DIR = BIRD_DIR / "results"
DB_ROOT = DEV_DIR / "databases" / "dev_databases"

# Load env
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

# Load data
with open(DEV_DIR / "dev.json") as f:
    dev_data = json.load(f)
with open(DEV_DIR / "dev_tables.json") as f:
    tables_data = json.load(f)
with open(RESULTS_DIR / "sample_indices.json") as f:
    sample_indices = json.load(f)

# Load existing predictions
predictions = {}
pred_path = RESULTS_DIR / "predict_stratified.json"
if pred_path.exists():
    with open(pred_path) as f:
        predictions = json.load(f)

# Build schema cache
schema_cache = {}
for t in tables_data:
    db_id = t["db_id"]
    table_names = t["table_names_original"]
    col_names = t["column_names_original"]
    col_types = t["column_types"]
    
    tables = {}
    for i, (table_idx, col_name) in enumerate(col_names):
        if table_idx == -1:
            continue
        if table_idx not in tables:
            tables[table_idx] = []
        tables[table_idx].append((col_name, col_types[i] if i < len(col_types) else "text"))
    
    lines = []
    for ti in sorted(tables.keys()):
        tn = table_names[ti]
        lines.append(f"CREATE TABLE {tn} (")
        col_lines = [f"  {cn} {ct}" for cn, ct in tables[ti]]
        lines.append(",\n".join(col_lines))
        lines.append(")")
    
    schema_cache[db_id] = "\n\n".join(lines) + "\n"

# Init provider
provider = DeepSeekProvider()
print(f"Running stratified eval: {len(sample_indices)} questions across 11 databases")
print(f"Already completed: {len(predictions)}")
print()

to_run = [i for i in sample_indices if str(i) not in predictions]
print(f"Remaining to run: {len(to_run)}")
print()

start_time = time.time()
for batch_idx, idx in enumerate(to_run):
    q = dev_data[idx]
    db_id = q["db_id"]
    schema = schema_cache.get(db_id, "")
    evidence = q.get("evidence", "")
    
    prompt_parts = [
        "You are a SQLite expert. Generate a single SELECT statement.",
        "",
        "Database Schema:",
        schema,
    ]
    if evidence:
        prompt_parts.extend(["", f"Hint: {evidence}"])
    prompt_parts.extend([
        "",
        f"Question: {q['question']}",
        "",
        "Return ONLY valid JSON: {\"sql\": \"SELECT...\", \"assumptions\": [], \"tables_used\": [], \"columns_used\": [], \"confidence\": \"high|medium|low\", \"reasoning_summary\": \"\"}",
        "Use SQLite syntax. Backtick-quote identifiers with spaces.",
    ])
    prompt = "\n".join(prompt_parts)
    
    system = "You are a SQLite expert. Generate exactly one SELECT. No SELECT *. Return valid JSON."
    
    try:
        raw = provider.generate(f"{system}\n\n{prompt}")
        
        # Extract SQL from JSON
        sql = None
        start = raw.find("{")
        if start >= 0:
            depth = 0
            in_str = False
            quote = ""
            for i in range(start, len(raw)):
                c = raw[i]
                if in_str:
                    if c == "\\": pass  # skip next
                    elif c == quote: in_str = False
                elif c in ("'", '"'):
                    in_str = True
                    quote = c
                elif c == "{": depth += 1
                elif c == "}": 
                    depth -= 1
                    if depth == 0:
                        candidate = raw[start:i+1]
                        import re
                        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
                        data = json.loads(candidate)
                        if "sql" in data:
                            sql = data["sql"]
                        break
        
        if sql:
            predictions[str(idx)] = f"{sql}\t----- bird -----\t{db_id}"
            status = "✓"
        else:
            predictions[str(idx)] = ""
            status = "✗"
        
        elapsed = time.time() - start_time
        rate = (batch_idx + 1) / elapsed * 60 if elapsed > 0 else 0
        print(f"  [{batch_idx+1}/{len(to_run)}] Q{idx} {db_id:30} {status} ({rate:.0f}/min)", flush=True)
    
    except Exception as e:
        predictions[str(idx)] = ""
        print(f"  [{batch_idx+1}/{len(to_run)}] Q{idx} {db_id:30} ✗ ERROR: {str(e)[:60]}", flush=True)
    
    # Save every 10
    if (batch_idx + 1) % 10 == 0:
        with open(pred_path, "w") as f:
            json.dump(predictions, f, indent=2)

# Final save
with open(pred_path, "w") as f:
    json.dump(predictions, f, indent=2)

elapsed = time.time() - start_time
completed = sum(1 for v in predictions.values() if v and v != "")
print(f"\n{'='*60}")
print(f"Done. {completed}/{len(sample_indices)} completed in {elapsed/60:.1f} min")
print(f"Results saved to {pred_path}")
