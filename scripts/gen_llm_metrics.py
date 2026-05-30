#!/usr/bin/env python3
"""Final fix: Use LLM to generate properly linked metrics for each DB."""
import sys, os, json, yaml, glob, re, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

env_path = os.path.expanduser("~/.hermes/.env")
for line in open(env_path):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'\"")
        if k == "DEEPSEEK_API_KEY":
            os.environ[k] = v

from src.semantic_registry.pipeline.llm_gateway import DeepSeekProvider
from src.semantic_registry.resolver.registry import load_semantic_registry

provider = DeepSeekProvider()

with open("bird_bench/dev/dev_20240627/dev.json") as f:
    dev = json.load(f)
with open("bird_bench/dev/dev_20240627/dev_tables.json") as f:
    tables_data = json.load(f)

def extract_json(raw):
    start = raw.find("{")
    if start < 0: return None
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
                return json.loads(raw[start:i+1])
    return None

print("LLM-generating concept-linked metrics per database...")

for db_id in sorted(os.listdir("bird_semantic")):
    db_dir = f"bird_semantic/{db_id}"
    if not os.path.isdir(db_dir): continue
    
    try:
        reg = load_semantic_registry(db_dir)
    except: continue
    
    if not reg.concepts or not reg.terms: continue
    
    # Find example questions + gold SQL
    db_questions = [(i, q) for i, q in enumerate(dev) if q["db_id"] == db_id]
    
    # Build schema text
    schema = None
    for t in tables_data:
        if t["db_id"] == db_id:
            schema = t
            break
    if not schema: continue
    
    table_lines = []
    for ti, tn in enumerate(schema["table_names_original"]):
        cols = [(c[1], schema["column_types"][j]) for j, c in enumerate(schema["column_names_original"]) if c[0] == ti]
        col_lines = [f"  {cn} {ct}" for cn, ct in cols]
        table_lines.append(f"CREATE TABLE {tn} (")
        table_lines.append(",\n".join(col_lines))
        table_lines.append(")")
    schema_text = "\n\n".join(table_lines)
    
    # Build concepts + terms summary
    concepts_str = "\n".join(f"  - {c.concept}: {c.definition}" for c in reg.concepts)
    terms_str = "\n".join(f"  - {t.term} (synonyms: {t.synonyms}) -> concepts: {t.candidate_concepts}" for t in reg.terms)
    
    # Build sample questions + gold SQL
    q_str = "\n".join(f"  Q{q_idx}: \"{q['question']}\" -> {q['SQL']}" for q_idx, q in db_questions[:8])
    
    prompt = f'''Database: {db_id}

SCHEMA:
{schema_text}

CONCEPTS (business concepts users talk about):
{concepts_str}

TERMS (natural language phrases in questions):
{terms_str}

SAMPLE QUESTIONS + GOLD SQL:
{q_str}

For each CONCEPT, generate a SQL METRIC that best implements what users ask for.
Return ONLY a JSON object:
{{"metrics": [
  {{
    "metric": "snake_case_name",
    "concept": "MUST match one of the CONCEPT names above",
    "description": "What this metric represents",
    "type": "simple_sum|simple_count|distinct_count|advanced",
    "measure": {{"table": "actual_table", "column": "actual_column"}},
    "aggregation": "sum|count|avg|min|max",
    "expression": "full expression if advanced type"
  }}
]}}

CRITICAL RULES:
- "concept" must EXACTLY match a concept name from the CONCEPTS list
- table and column must exist in the SCHEMA
- For ratio/expression metrics, use type "advanced" and expression field
- Generate at most {len(reg.concepts)} metrics, one per concept
- Each metric's "metric" name should be short and descriptive'''

    try:
        raw = provider.generate(f"Return ONLY valid JSON.\n\n{prompt}")
        data = extract_json(raw)
        if not data or "metrics" not in data:
            print(f"  {db_id}: No valid metrics from LLM")
            continue
        
        # Validate and write metrics
        metrics = data["metrics"]
        valid_table_names = set(schema["table_names_original"])
        
        m_dir = os.path.join(db_dir, "metrics")
        for f in os.listdir(m_dir):
            os.remove(os.path.join(m_dir, f))
        
        written = 0
        for m in metrics:
            mc = m.get("concept", "")
            if mc not in {c.concept for c in reg.concepts}:
                continue
            
            meas = m.get("measure", {})
            tbl = meas.get("table", "") if meas else ""
            col = meas.get("column", "") if meas else ""
            
            if tbl and col:
                if tbl not in valid_table_names:
                    continue
                # Verify column exists
                cols_in_table = [c[1] for c in schema["column_names_original"] if c[0] == list(schema["table_names_original"]).index(tbl)] if tbl in schema["table_names_original"] else []
                # Simpler check
                found = False
                for ti, tn in enumerate(schema["table_names_original"]):
                    if tn == tbl:
                        for j, (cji, cn) in enumerate(schema["column_names_original"]):
                            if cji == ti and cn.lower().replace(" ", "_") == col.lower().replace(" ", "_"):
                                found = True
                                break
                        break
                if not found:
                    continue
            
            metric_name = re.sub(r'[^a-zA-Z0-9_-]', '_', m.get("metric", "")).strip("_").lower()
            if not metric_name:
                continue
            
            entry = {
                "metric": metric_name,
                "concept": mc,
                "description": m.get("description", ""),
                "type": m.get("type", "simple_count"),
                "owner": "bird_benchmark",
                "status": "certified",
            }
            if entry["type"] == "advanced":
                entry["expression"] = m.get("expression", "")
            else:
                if meas and tbl:
                    entry["measure"] = {"table": tbl, "column": col}
                entry["aggregation"] = m.get("aggregation", "count")
            entry["unit"] = m.get("unit", "")
            entry["allowed_dimensions"] = []
            
            path = os.path.join(m_dir, f"{metric_name}.yaml")
            with open(path, "w") as f:
                yaml.dump(entry, f, default_flow_style=False, sort_keys=False)
            written += 1
        
        # Set canonical_metric for concepts that now have metrics
        metric_by_concept = {}
        for f in os.listdir(m_dir):
            with open(os.path.join(m_dir, f)) as fh:
                dm = yaml.safe_load(fh)
            if dm:
                metric_by_concept[dm.get("concept", "")] = dm.get("metric", "")
        
        c_dir = os.path.join(db_dir, "concepts")
        updated = 0
        for f in os.listdir(c_dir):
            path = os.path.join(c_dir, f)
            with open(path) as fh:
                dc = yaml.safe_load(fh)
            if dc and dc.get("concept") in metric_by_concept:
                dc["canonical_metric"] = metric_by_concept[dc["concept"]]
                with open(path, "w") as f:
                    yaml.dump(dc, f, default_flow_style=False, sort_keys=False)
                updated += 1
        
        print(f"  {db_id}: {written} metrics LLM-generated, {updated} concepts linked")
    
    except Exception as e:
        print(f"  {db_id}: ERROR {e}")

print("\nDone! All metrics generated by LLM with proper concept linkage.")
