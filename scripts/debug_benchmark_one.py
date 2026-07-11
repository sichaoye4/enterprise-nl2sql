"""
Run the exact benchmark evaluation function on 1 question and dump everything.
"""
import json, os, sys, time, sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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

db_id = "california_schools"
registry = load_semantic_registry(ROOT / "bird_semantic" / db_id)
pipeline = NL2SQLPipeline(
    registry_data=registry,
    semantic_model_path=ROOT / "bird_semantic_engine",
)

dev = json.loads((ROOT / "bird_bench/dev/dev_20240627/dev.json").read_text())
q = dev[0]

db_path = ROOT / "bird_bench/dev/dev_20240627/databases/dev_databases" / db_id / f"{db_id}.sqlite"

print("=" * 60)
print(f"Question: {q['question']}")
print(f"Gold SQL: {q['SQL']}")
print(f"DB path: {db_path}")
print()

context = pipeline.run(q["question"], domain=db_id)

# Exact same logic as benchmark's selected_sql()
def selected_sql(context):
    if getattr(context, "response", None) is not None and context.response.generated_sql:
        return context.response.generated_sql
    if getattr(context, "selected_sql", None) is not None and context.selected_sql.sql:
        return context.selected_sql.sql
    for candidate in getattr(context, "sql_candidates", []) or []:
        if candidate.sql:
            return candidate.sql
    return ""

sql = selected_sql(context)
print(f"selected_sql(): {sql}")
print(f"context.response.generated_sql: {getattr(context.response, 'generated_sql', 'N/A')}")
print(f"context.selected_sql.sql: {getattr(context.selected_sql, 'sql', 'N/A') if context.selected_sql else 'NONE'}")
print(f"context.selected_sql.parse_success: {getattr(context.selected_sql, 'parse_success', 'N/A') if context.selected_sql else 'NONE'}")
print(f"context.selected_sql.validation_errors: {getattr(context.selected_sql, 'validation_errors', 'N/A') if context.selected_sql else 'NONE'}")
print()

# Execute both
conn = sqlite3.connect(str(db_path))
try:
    c = conn.execute(sql)
    pred = c.fetchall()
    print(f"Pipeline result: {pred}")
except Exception as e:
    print(f"Pipeline ERROR: {e}")
    pred = []

try:
    c = conn.execute(q["SQL"])
    gold = c.fetchall()
    print(f"Gold result: {gold}")
except Exception as e:
    print(f"Gold ERROR: {e}")
    gold = []

conn.close()

if pred and gold:
    match = set(tuple(str(v).strip() for v in r) for r in pred) == set(tuple(str(v).strip() for v in r) for r in gold)
    print(f"\nMatch: {match}")
    if not match:
        print(f"Pipeline set: {set(tuple(str(v).strip() for v in r) for r in pred)}")
        print(f"Gold set: {set(tuple(str(v).strip() for v in r) for r in gold)}")
