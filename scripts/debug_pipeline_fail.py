"""
Debug: run a single question through the full pipeline and dump what happens.
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

from src.semantic_registry.resolver.registry import load_semantic_registry
from src.semantic_registry.pipeline import NL2SQLPipeline
from src.semantic_registry.pipeline.llm_gateway import DeepSeekProvider

# Test question 0 from the sample
db_id = "california_schools"
registry = load_semantic_registry(ROOT / "bird_semantic" / db_id)
print(f"Registry: {len(registry.metrics)} metrics, {len(registry.concepts)} concepts")

pipeline = NL2SQLPipeline(
    registry_data=registry,
    semantic_model_path=ROOT / "bird_semantic_engine",
)

dev = json.loads((ROOT / "bird_bench/dev/dev_20240627/dev.json").read_text())
q = dev[0]  # First sample question
print(f"\nQuestion [{q['question_id']}]: {q['question']}")
print(f"Gold SQL: {q['SQL'][:150]}")

started = time.time()
context = pipeline.run(q["question"], domain=db_id)
elapsed = time.time() - started

print(f"\nElapsed: {elapsed:.1f}s")
print(f"Route: {context.semantic_route}")
print(f"Trace: {context.trace}")
print(f"Error: {context.error}")
print(f"Requires clarification: {context.requires_clarification}")

if context.selected_sql:
    print(f"\n=== Selected SQL ===")
    print(f"SQL: {context.selected_sql.sql}")
    print(f"Strategy: {context.selected_sql.generation_strategy}")
    print(f"Confidence: {context.selected_sql.confidence}")
    print(f"Parse success: {context.selected_sql.parse_success}")
    print(f"Validation errors: {context.selected_sql.validation_errors}")
    print(f"Assumptions: {context.selected_sql.assumptions}")

if context.response:
    print(f"\n=== Response ===")
    print(f"SQL: {context.response.generated_sql}")
    print(f"Assumptions: {context.response.assumptions}")

if context.llm_judge_result:
    print(f"\n=== LLM Judge ===")
    print(json.dumps(context.llm_judge_result, indent=2))

# Also try direct DeepSeek call to compare what the LLM generates standalone
print(f"\n\n=== Direct DeepSeek baseline (no pipeline) ===")
from src.semantic_registry.pipeline.llm_gateway import DeepSeekProvider
# Build a minimal schema prompt (like scripts/bird_eval.py does)
from scripts.bird_eval import build_schema_prompt, build_prompt
tables_data = json.loads((ROOT / "bird_bench/dev/dev_20240627/dev_tables.json").read_text())
schema = build_schema_prompt(tables_data, db_id)
evidence = q.get("evidence", "")
prompt = build_prompt(q["question"], schema, evidence)
print(f"Prompt length: {len(prompt)} chars")
provider = DeepSeekProvider(model="deepseek-chat")
raw = provider.generate(prompt)
print(f"Raw response[:200]: {raw[:200]}")
