"""
Debug: run 2 BIRD questions, capture full LLM trace, compare to baseline.
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

from src.semantic_registry.pipeline import (
    CandidateGenerator, DeepSeekProvider, LLMGateway, LLMJudge, NL2SQLPipeline
)
from src.semantic_registry.resolver.registry import load_semantic_registry

# Split model config
router_gateway = LLMGateway(provider=DeepSeekProvider(model="deepseek-v4-flash", reasoning_effort=None))
gen_gateway = LLMGateway(provider=DeepSeekProvider(model="deepseek-v4-pro", reasoning_effort="high"))
judge_client = DeepSeekProvider(model="deepseek-v4-pro", reasoning_effort="high")
judge = LLMJudge(client=judge_client)

# Pick TWO questions: one from debit (should be easy) and one complex
db_id = "debit_card_specializing"
registry = load_semantic_registry(ROOT / "bird_semantic" / db_id)

dev = json.loads((ROOT / "bird_bench/dev/dev_20240627/dev.json").read_text())
db_qs = [q for q in dev if q["db_id"] == db_id]

# Q1470: simple COUNT + WHERE (should be SEMANTIC_SQL via router)
# Q1510: AVG price + filter
test_qs = [db_qs[0], db_qs[33]]

for i, q in enumerate(test_qs):
    print(f"\n{'='*70}")
    print(f"[{i+1}/2] Q{q['question_id']}: {q['question']}")
    print(f"  Gold SQL: {q['SQL'][:120]}")

    # Create pipeline per question (to avoid state bleed)
    pipeline = NL2SQLPipeline(
        registry_data=registry,
        semantic_model_path=ROOT / "bird_semantic_engine",
        router_llm_gateway=router_gateway,
        candidate_generator=CandidateGenerator(llm_gateway=gen_gateway),
        llm_judge=judge,
    )
    
    started = time.time()
    context = pipeline.run(q["question"], domain=db_id)
    elapsed = time.time() - started
    
    print(f"\n  Time: {elapsed:.0f}s")
    print(f"  Route: {context.semantic_route}")
    print(f"  Trace: {context.trace}")
    print(f"  Error: {context.error}")
    
    # Get SQL
    sql = ""
    if context.response and context.response.generated_sql:
        sql = context.response.generated_sql
    elif context.selected_sql and context.selected_sql.sql:
        sql = context.selected_sql.sql
    print(f"  Generated SQL: {sql[:150] if sql else '(NONE)'}")
    
    # LLM Trace - dump EVERYTHING
    if context.llm_trace:
        print(f"\n  LLM TRACE ({len(context.llm_trace)} entries):")
        for stage, entry in sorted(context.llm_trace.items()):
            prompt = (entry.get("prompt") or "")[:300]
            response = (entry.get("response") or "")[:300]
            print(f"\n  --- {stage} ---")
            print(f"  PROMPT[:300]: {prompt}")
            print(f"  RESPONSE[:300]: {response}")
    
    # Execute and compare
    if sql:
        db_path = ROOT / "bird_bench/dev/dev_20240627/databases/dev_databases" / db_id / f"{db_id}.sqlite"
        conn = sqlite3.connect(str(db_path))
        try:
            c = conn.execute(sql)
            pred = c.fetchall()
            c = conn.execute(q["SQL"])
            gold = c.fetchall()
            match = set(tuple(str(v).strip() for v in r) for r in pred) == set(tuple(str(v).strip() for v in r) for r in gold)
            print(f"\n  Result: {pred}")
            print(f"  Gold:   {gold}")
            print(f"  {'✅ MATCH' if match else '❌ MISMATCH'}")
        except Exception as e:
            print(f"\n  SQL ERROR: {e}")
        finally:
            conn.close()
    
    # Also run a BASELINE comparison: simple prompt (no pipeline)
    print(f"\n  --- BASELINE COMPARISON (simple prompt) ---")
    from scripts.bird_eval import build_schema_prompt, build_prompt
    tables_data = json.loads((ROOT / "bird_bench/dev/dev_20240627/dev_tables.json").read_text())
    schema = build_schema_prompt(tables_data, db_id)
    evidence = q.get("evidence", "")
    baseline_prompt = build_prompt(q["question"], schema, evidence)
    
    # Use the SAME generation model (V4 Pro) for fair comparison
    provider = DeepSeekProvider(model="deepseek-v4-pro", reasoning_effort="high")
    raw = provider.generate(baseline_prompt)
    print(f"  Baseline prompt length: {len(baseline_prompt)} chars")
    print(f"  Baseline raw response[:300]: {raw[:300]}")
    
    # Extract SQL from baseline
    try:
        baseline_data = json.loads(raw[raw.find("{"):raw.rfind("}")+1])
        baseline_sql = baseline_data.get("sql", "")
    except:
        baseline_sql = raw
    
    if baseline_sql:
        db_path = ROOT / "bird_bench/dev/dev_20240627/databases/dev_databases" / db_id / f"{db_id}.sqlite"
        conn = sqlite3.connect(str(db_path))
        try:
            c = conn.execute(baseline_sql)
            pred = c.fetchall()
            c = conn.execute(q["SQL"])
            gold = c.fetchall()
            match = set(tuple(str(v).strip() for v in r) for r in pred) == set(tuple(str(v).strip() for v in r) for r in gold)
            print(f"  Baseline SQL: {baseline_sql[:120]}")
            print(f"  Baseline result: {pred}")
            print(f"  {'✅ BASELINE MATCH' if match else '❌ BASELINE MISMATCH'}")
        except Exception as e:
            print(f"  Baseline SQL ERROR: {e}")
        finally:
            conn.close()
