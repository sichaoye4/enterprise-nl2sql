"""
Quick test: run 3 BIRD questions through the updated pipeline.
Tests: semantic engine, router, judge, LLM fallback.
"""
import json, os, sys, time, sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env_path = Path.home() / ".hermes" / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))

from src.semantic_registry.resolver.registry import load_semantic_registry
from src.semantic_registry.pipeline import NL2SQLPipeline

# Test 3 questions from debit_card_specializing (our best DB)
db_id = "debit_card_specializing"
registry = load_semantic_registry(ROOT / "bird_semantic" / db_id)
pipeline = NL2SQLPipeline(registry_data=registry, semantic_model_path=ROOT / "bird_semantic_engine")

dev = json.loads((ROOT / "bird_bench/dev/dev_20240627/dev.json").read_text())
db_qs = [q for q in dev if q["db_id"] == db_id]

# Pick 3 representative questions
test_qs = [
    db_qs[0],   # Q1470: simple COUNT + WHERE (should be SEMANTIC_SQL via router)
    db_qs[13],  # Q1483: SUM + between dates
    db_qs[33],  # Q1504: AVG price + filter
]

db_path = ROOT / "bird_bench/dev/dev_20240627/databases/dev_databases" / db_id / f"{db_id}.sqlite"

for i, q in enumerate(test_qs):
    print(f"\n{'='*60}")
    print(f"[{i+1}/3] Q{q['question_id']}: {q['question']}")
    print(f"  Difficulty: {q.get('difficulty','?')}")
    print(f"  Gold: {q['SQL'][:100]}")

    started = time.time()
    context = pipeline.run(q["question"], domain=db_id)
    elapsed = time.time() - started

    print(f"  Time: {elapsed:.0f}s")
    print(f"  Route: {context.semantic_route}")
    print(f"  Trace: {context.trace}")
    print(f"  Error: {context.error}")
    print(f"  Judge: {context.llm_judge_result.get('pass','N/A') if context.llm_judge_result else 'N/A'}")

    # Get SQL
    sql = ""
    if context.response and context.response.generated_sql:
        sql = context.response.generated_sql
    elif context.selected_sql and context.selected_sql.sql:
        sql = context.selected_sql.sql
    print(f"  SQL: {sql[:120] if sql else '(none)'}")

    if sql:
        conn = sqlite3.connect(str(db_path))
        try:
            c = conn.execute(sql)
            pred = c.fetchall()
            c = conn.execute(q["SQL"])
            gold = c.fetchall()
            match = set(tuple(str(v).strip() for v in r) for r in pred) == set(tuple(str(v).strip() for v in r) for r in gold)
            print(f"  Result: {pred}")
            print(f"  Gold:   {gold}")
            print(f"  {'✅ MATCH' if match else '❌ MISMATCH'}")
        except Exception as e:
            print(f"  SQL ERROR: {e}")
        finally:
            conn.close()

print(f"\n{'='*60}")
print("Done. Ready for full benchmark?")
