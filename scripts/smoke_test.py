"""
Quick smoke test: run the full NL2SQL pipeline on 1 BIRD question.
Tests: DeepSeek API connectivity, pipeline stages, SQL execution.
"""
import json, os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load API keys
env_path = Path.home() / ".hermes" / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))

print(f"DEEPSEEK_API_KEY: {bool(os.environ.get('DEEPSEEK_API_KEY'))}")
print(f"DASHSCOPE_API_KEY: {bool(os.environ.get('DASHSCOPE_API_KEY'))}")

from src.semantic_registry.resolver.registry import load_semantic_registry
from src.semantic_registry.pipeline import NL2SQLPipeline

# Load registry + create pipeline
db_id = "debit_card_specializing"
registry = load_semantic_registry(ROOT / "bird_semantic" / db_id)
print(f"\nRegistry loaded: {len(registry.metrics)} metrics, {len(registry.concepts)} concepts")

pipeline = NL2SQLPipeline(
    registry_data=registry,
    semantic_model_path=ROOT / "bird_semantic_engine",
)

# Load questions
dev = json.loads((ROOT / "bird_bench/dev/dev_20240627/dev.json").read_text())
db_qs = [q for q in dev if q["db_id"] == db_id]

# Test 1470: "How many gas stations in CZE has Premium gas?"
q = db_qs[0]  # Q1470
print(f"\nTest: [{q['question_id']}] {q['question']}")
print(f"Gold SQL: {q['SQL']}")
print()

started = time.time()
context = pipeline.run(q["question"], domain=db_id)
elapsed = time.time() - started

print(f"Elapsed: {elapsed:.1f}s")
print(f"Semantic route: {context.semantic_route}")
print(f"Trace: {context.trace}")
print(f"Error: {context.error}")
print(f"Requires clarification: {context.requires_clarification}")

if context.selected_sql:
    print(f"\nSelected SQL: {context.selected_sql.sql}")
    print(f"AI confidence: {context.selected_sql.confidence}")
    print(f"Generation strategy: {context.selected_sql.generation_strategy}")

if context.response:
    print(f"\nResponse SQL: {context.response.generated_sql}")
    print(f"Response assumptions: {context.response.assumptions}")

if context.llm_judge_result:
    print(f"\nLLM Judge: pass={context.llm_judge_result.get('pass')}, "
          f"reasoning={context.llm_judge_result.get('reasoning', '')[:80]}")
