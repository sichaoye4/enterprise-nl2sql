#!/usr/bin/env python3
"""Rebuild deduplicated metrics + relink concepts for all BIRD databases."""
import sys, os, json, yaml, glob, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from collections import defaultdict

with open("bird_bench/dev/dev_20240627/dev.json") as f:
    dev = json.load(f)
with open("bird_bench/dev/dev_20240627/dev_tables.json") as f:
    tables_data = json.load(f)

def get_columns(db_id):
    for t in tables_data:
        if t["db_id"] == db_id:
            cols = defaultdict(list)
            for i, (ti, cn) in enumerate(t["column_names_original"]):
                if ti == -1: continue
                cols[t["table_names_original"][ti]].append(cn)
            return cols
    return {}

def normalize_col(col):
    return col.lower().replace(" ", "_").replace("`", "")

def find_table(col_norm, table_cols):
    for tn, cols in table_cols.items():
        for rc in cols:
            if rc.lower().replace(" ", "_") == col_norm or rc.lower() == col_norm.replace("_", " "):
                return tn, rc
    return None, None

# Regex patterns for SQL aggregation extraction
AGG_PATTERNS = [
    (r'COUNT\s*\(\s*DISTINCT\s+(?:\w+\.)?`?(\w+(?:\s+\w+)*)`?\s*\)', 'count_distinct'),
    (r'COUNT\s*\(\s*(?:\w+\.)?`?(\w+(?:\s+\w+)*)`?\s*\)', 'count'),
    (r'SUM\s*\(\s*(?:\w+\.)?`?(\w+(?:\s+\w+)*)`?\s*\)', 'sum'),
    (r'AVG\s*\(\s*(?:\w+\.)?`?(\w+(?:\s+\w+)*)`?\s*\)', 'avg'),
    (r'MIN\s*\(\s*(?:\w+\.)?`?(\w+(?:\s+\w+)*)`?\s*\)', 'min'),
    (r'MAX\s*\(\s*(?:\w+\.)?`?(\w+(?:\s+\w+)*)`?\s*\)', 'max'),
]

TYPE_MAP = {
    'sum': 'simple_sum', 'count': 'simple_count',
    'count_distinct': 'distinct_count', 'avg': 'advanced',
    'min': 'advanced', 'max': 'advanced',
}

print("Rebuilding metrics for all databases...")

for db_id in sorted(os.listdir("bird_semantic")):
    db_dir = f"bird_semantic/{db_id}"
    if not os.path.isdir(db_dir):
        continue
    
    db_questions = [(i, q) for i, q in enumerate(dev) if q["db_id"] == db_id]
    table_cols = get_columns(db_id)
    
    # Extract deduplicated metrics
    seen = {}  # (agg_type, column_norm, table_name) -> metric_data
    
    for idx, q in db_questions:
        sql = q["SQL"]
        
        # Aggregation metrics
        for pattern, agg_type in AGG_PATTERNS:
            for m in re.finditer(pattern, sql, re.IGNORECASE):
                col_raw = m.group(1).replace("`", "")
                col_norm = normalize_col(col_raw)
                tn, rc = find_table(col_norm, table_cols)
                if tn is None:
                    continue
                key = (agg_type, col_norm, tn)
                if key not in seen:
                    seen[key] = {
                        "metric": f"{agg_type}_{tn}_{col_norm}",
                        "concept": f"{db_id}_{agg_type}",
                        "description": f"{agg_type.upper()} of {col_raw} in {tn}",
                        "type": TYPE_MAP.get(agg_type, "simple_count"),
                        "measure": {"table": tn, "column": rc},
                        "aggregation": agg_type,
                        "unit": "",
                        "allowed_dimensions": [],
                        "owner": "bird_benchmark",
                        "status": "certified",
                    }
        
        # Ratio metrics
        ratio_pat = r'(?:\w+\.)?`?(\w+(?:\s+\w+)*)`?\s*/\s*(?:\w+\.)?`?(\w+(?:\s+\w+)*)`?'
        for m in re.finditer(ratio_pat, sql, re.IGNORECASE):
            num = m.group(1).replace("`", "")
            den = m.group(2).replace("`", "")
            nn, dn = normalize_col(num), normalize_col(den)
            key = ("ratio", f"{nn}_over_{dn}", db_id)
            if key not in seen:
                select_part = sql.split(" FROM ")[0] if " FROM " in sql.upper() else sql[:100]
                seen[key] = {
                    "metric": f"ratio_{nn}_over_{dn}",
                    "concept": f"{db_id}_ratio",
                    "description": f"Ratio of {num} to {den}",
                    "type": "advanced",
                    "expression": select_part,
                    "unit": "",
                    "allowed_dimensions": [],
                    "owner": "bird_benchmark",
                    "status": "certified",
                }
    
    metrics = list(seen.values())
    
    # Clean and rewrite metric files
    m_dir = os.path.join(db_dir, "metrics")
    for f in os.listdir(m_dir):
        os.remove(os.path.join(m_dir, f))
    
    for m in metrics:
        safe = re.sub(r'[^a-zA-Z0-9_-]', '_', m["metric"]).strip("_").lower()
        path = os.path.join(m_dir, f"{safe}.yaml")
        with open(path, "w") as f:
            yaml.dump(m, f, default_flow_style=False, sort_keys=False)
    
    # Relink concepts
    c_dir = os.path.join(db_dir, "concepts")
    linked, total = 0, 0
    metric_names = {m["metric"]: m for m in metrics}
    
    for f_name in os.listdir(c_dir):
        path = os.path.join(c_dir, f_name)
        with open(path) as f:
            c = yaml.safe_load(f)
        if not c:
            continue
        total += 1
        c_name = c.get("concept", "")
        if not c_name:
            continue
        
        # Find terms referencing this concept
        t_dir = os.path.join(db_dir, "terms")
        related_terms = []
        if os.path.exists(t_dir):
            for tf in os.listdir(t_dir):
                with open(os.path.join(t_dir, tf)) as f:
                    t = yaml.safe_load(f)
                if not t:
                    continue
                if c_name in t.get("candidate_concepts", []) or c_name in t.get("default_concept_by_domain", {}).values():
                    related_terms.append(t["term"])
        
        best_metric, best_score = None, 0
        c_tokens = set(c_name.lower().split("_"))
        
        for t_name in related_terms:
            t_norm = t_name.lower().replace("_", " ")
            for idx, q in db_questions:
                if t_norm not in q["question"].lower() and t_norm not in q.get("evidence", "").lower():
                    continue
                
                # Match gold SQL aggs to metrics
                for pattern, agg_type in AGG_PATTERNS:
                    for m in re.finditer(pattern, q["SQL"], re.IGNORECASE):
                        col_raw = m.group(1).replace("`", "")
                        col_norm = normalize_col(col_raw)
                        tn, rc = find_table(col_norm, table_cols)
                        if tn is None:
                            continue
                        mn = f"{agg_type}_{tn}_{col_norm}"
                        if mn in metric_names:
                            score = 3 if agg_type != "count" else 2
                            if score > best_score:
                                best_score = score
                                best_metric = mn
        
        if best_metric:
            c["canonical_metric"] = best_metric
            with open(path, "w") as f:
                yaml.dump(c, f, default_flow_style=False, sort_keys=False)
            linked += 1
    
    print(f"  {db_id:30} {len(metrics):>3} metrics, {linked}/{total} concepts linked")

print("\nDone!")
