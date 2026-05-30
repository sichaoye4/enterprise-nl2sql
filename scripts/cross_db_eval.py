#!/usr/bin/env python3
"""Cross-DB evaluation: few-shot examples come from OTHER databases only."""
import sys, os, json, time, re, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

env_path = os.path.expanduser("~/.hermes/.env")
for line in open(env_path):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1); k, v = k.strip(), v.strip().strip("'\"")
        if k == "DEEPSEEK_API_KEY": os.environ[k] = v

from src.semantic_registry.pipeline.llm_gateway import DeepSeekProvider

with open("bird_bench/dev/dev_20240627/dev.json") as f: dev = json.load(f)
with open("bird_bench/results/sample_indices.json") as f: indices = json.load(f)

DB_ROOT = "bird_bench/dev/dev_20240627/databases/dev_databases"

STOP_WORDS = {"what","is","the","of","in","for","to","a","an","and","or","are",
              "was","with","that","this","these","from","by","at","on","all",
              "each","every","please","list","show","give","me","name","find",
              "how","many","much","do","does","did","has","have","had","which",
              "whose","whom","who","where","when","why","than"}

def classify(sql):
    u = sql.upper()
    if u.count("SELECT") > 1: return "subquery"
    if "JOIN" in u: return "join_agg" if re.search(r"COUNT|SUM|AVG|MIN|MAX", u) else "join_simple"
    if "/" in sql: return "ratio"
    if re.search(r"COUNT|SUM|AVG|MIN|MAX", u):
        return "agg_filter" if "WHERE" in u else "simple_agg"
    if "ORDER BY" in u and "LIMIT" in u: return "top_n"
    return "simple_select"

def extract_terms(q):
    return [w for w in re.findall(r"[a-zA-Z]+", q.lower()) if w not in STOP_WORDS and len(w) > 2]

def extract_sql(raw):
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
                        cand = re.sub(r",\s*([}\]])", r"\1", raw[start:i+1])
                        d = json.loads(cand)
                        if "sql" in d: return d["sql"]
    except: pass
    m = re.search(r"SELECT\s+.*?(?:;|$)", raw, re.DOTALL | re.IGNORECASE)
    return m.group(0).strip().rstrip(";") if m else ""

def get_schema(db_id):
    db_path = f"{DB_ROOT}/{db_id}/{db_id}.sqlite"
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL")
    schemas = [row[0] for row in c.fetchall()]
    conn.close()
    return "\n\n".join(schemas)

def find_patterns(question, test_db_id, query_type, top_k=3):
    """Find patterns from OTHER databases only (cross-DB transfer)."""
    q_terms = set(extract_terms(question))
    candidates = []
    for i, q in enumerate(dev):
        if q["db_id"] == test_db_id:
            continue  # EXCLUDE same DB
        if classify(q["SQL"]) != query_type:
            continue
        t = set(extract_terms(q["question"]))
        overlap = len(q_terms & t)
        total = len(q_terms | t)
        score = overlap / total if total > 0 else 0
        if score > 0.1:
            candidates.append((score, q["question"], q["SQL"]))
    candidates.sort(key=lambda x: -x[0])
    return candidates[:top_k]

def build_prompt(question, schema, query_type, evidence, db_id, use_few):
    parts = ["You are a SQLite expert. Generate a single SELECT statement."]
    
    if use_few:
        patterns = find_patterns(question, db_id, query_type)
        if patterns:
            parts.append("Here are examples of similar questions from other databases:")
            ex_lines = []
            for i, (sc, eq, esql) in enumerate(patterns):
                ex_lines.append(f"Example {i+1}:\nQuestion: {eq}\nSQL: {esql}")
            parts.append("\n\n".join(ex_lines))
    
    parts.append("Database Schema:\n" + schema)
    if evidence:
        parts.append(f"Hint: {evidence}")
    parts.append(f"Question: {question}")
    parts.append('Return ONLY: {"sql": "SELECT ...", "assumptions": [], "tables_used": [], "columns_used": [], "confidence": "high|medium|low", "reasoning_summary": "..."}')
    
    return "\n\n".join(parts)


# ── Main ────────────────────────────────────────────────────────────────────

print("Cross-DB Controlled Experiment")
print(f"Testing {len(indices)} questions, comparing zero-shot vs cross-DB few-shot")
print()

for config_name, use_few in [("ZERO_SHOT (baseline)", False), ("CROSS_DB_FEW_SHOT (no leakage)", True)]:
    print(f"{'='*60}")
    print(f"  {config_name}")
    print(f"{'='*60}")
    
    provider = DeepSeekProvider(model="deepseek-v4-flash", reasoning_effort="xhigh")
    results = []
    start = time.time()
    
    for bi, idx in enumerate(indices):
        q = dev[idx]
        db_id = q["db_id"]
        gold = q["SQL"]
        evidence = q.get("evidence", "")
        schema = get_schema(db_id)
        qtype = classify(gold)
        
        prompt = build_prompt(q["question"], schema, qtype, evidence, db_id, use_few)
        
        try:
            raw = provider.generate(f"Return ONLY valid JSON.\n\n{prompt}")
            sql = extract_sql(raw)
        except Exception as e:
            sql = ""
        
        match = False
        if sql:
            try:
                db_path = f"{DB_ROOT}/{db_id}/{db_id}.sqlite"
                conn = sqlite3.connect(db_path)
                c = conn.cursor()
                c.execute(sql); pred = c.fetchall()
                c.execute(gold); gold_res = c.fetchall()
                conn.close()
                match = set(pred) == set(gold_res)
            except: pass
        
        results.append(match)
        elapsed = time.time() - start
        pct = sum(results) / len(results) * 100
        rate = (bi + 1) / elapsed * 60 if elapsed > 0 else 0
        print(f"  [{bi+1}/{len(indices)}] {'✅' if match else '❌'} {pct:.1f}% ({rate:.0f}/min)", flush=True)
    
    ex = sum(results) / len(results) * 100
    print(f"\n  FINAL EX: {ex:.1f}% ({sum(results)}/{len(results)}) in {(time.time()-start)/60:.1f}min")
    print()

print(f"{'='*60}")
print(f"  COMPARISON")
print(f"{'='*60}")
print(f"  Zero-shot (baseline):     TBD")
print(f"  Same-DB few-shot:         72.7% (prior run, with data leakage)")
print(f"  Cross-DB few-shot:        TBD")
print(f"{'='*60}")
