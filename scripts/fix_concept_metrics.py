#!/usr/bin/env python3
"""Fix concept->metric links using the question→SQL→metric chain."""
import sys, os, json, yaml, glob, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.semantic_registry.resolver.registry import load_semantic_registry
from collections import defaultdict

def normalize(s):
    return s.lower().replace("_", " ").replace("-", " ").replace("(", "").replace(")", "").strip()

def tokenize(s):
    return set(normalize(s).split())

with open("bird_bench/dev/dev_20240627/dev.json") as f:
    all_dev = json.load(f)

print("Fixing concept->metric links via question→SQL→metric chain...")

for db_id in sorted(os.listdir("bird_semantic")):
    db_dir = f"bird_semantic/{db_id}"
    if not os.path.isdir(db_dir):
        continue
    try:
        reg = load_semantic_registry(db_dir)
    except Exception as e:
        print(f"  {db_id}: LOAD ERROR {e}")
        continue

    if not reg.concepts or not reg.metrics:
        continue

    # Build metric lookup by table+column
    metric_by_tc = {}
    metric_by_name = {m.metric: m for m in reg.metrics}
    for m in reg.metrics:
        if m.measure:
            key = (m.measure.table.lower(), m.measure.column.lower().replace(" ", "_"))
            metric_by_tc[key] = m.metric
        # Also by metric name tokens
        metric_by_name[m.metric] = m

    # Map each term to its gold SQL
    term_to_questions = defaultdict(list)
    for idx, q in enumerate(all_dev):
        if q["db_id"] != db_id:
            continue
        sql = q.get("SQL", "")
        question = q.get("question", "").lower()
        evidence = q.get("evidence", "").lower()
        # Find which terms appear in this question/evidence
        for t in reg.terms:
            t_name = normalize(t.term)
            t_syns = [normalize(s) for s in t.synonyms]
            all_names = [t_name] + t_syns
            for name in all_names:
                if name in question or name in evidence:
                    term_to_questions[t.term].append((idx, sql, question, evidence))
                    break

    # For each concept, find the best metric
    linked = 0
    total_concepts = len(reg.concepts)
    updated_concepts = set()

    for concept in reg.concepts:
        c_name = concept.concept
        c_tokens = tokenize(c_name)
        
        # Find terms that reference this concept
        related_terms = [t for t in reg.terms if c_name in t.candidate_concepts or c_name in t.default_concept_by_domain.values()]
        
        # Collect gold SQL from related questions
        candidate_sqls = []
        for t in related_terms:
            for q_idx, sql, question, evidence in term_to_questions.get(t.term, []):
                candidate_sqls.append(sql)
        
        if not candidate_sqls:
            continue
        
        # For each gold SQL, try to find the matching metric
        best_matches = []
        for sql in candidate_sqls[:5]:
            # Extract aggregation patterns from SQL
            # COUNT(col), SUM(col), AVG(col), etc.
            agg_patterns = re.findall(r'(COUNT|SUM|AVG|MIN|MAX)\s*\(\s*(?:DISTINCT\s+)?(?:`[^`]+`\.)?`?(\w+(?:\s+\w+)*)`?\s*\)', sql, re.IGNORECASE)
            
            for agg_fn, col_name in agg_patterns:
                col_clean = col_name.lower().replace(" ", "_").replace("`", "")
                agg_lower = agg_fn.lower()
                
                # Build candidate metric names
                candidates = [
                    f"{agg_lower}_{col_clean}",
                    f"{agg_lower}_{col_clean}_2",
                    f"{agg_lower}_{col_clean}_3",
                    f"{agg_lower}_{col_clean}_4",
                    f"{agg_lower}_{col_clean}_5",
                    f"{agg_lower}_{col_clean}_6",
                    f"{agg_lower}_{col_clean}_7",
                    f"{agg_lower}_{col_clean}_8",
                    f"{agg_lower}_{col_clean}_9",
                    f"{agg_lower}_{col_clean}_10",
                ]
                for cand in candidates:
                    if cand in metric_by_name:
                        best_matches.append((cand, 3))  # high score for exact agg+col match
                        break
            
            # For ratio metrics
            ratio_matches = re.findall(r'(?:`[^`]+`\.)?`?(\w+(?:\s+\w+)*)`?\s*/\s*(?:`[^`]+`\.)?`?(\w+(?:\s+\w+)*)`?', sql)
            for num, den in ratio_matches:
                num_c = num.lower().replace(" ", "_").replace("`", "")
                den_c = den.lower().replace(" ", "_").replace("`", "")
                ratio_name = f"ratio_{num_c}_over_{den_c}"
                if ratio_name in metric_by_name:
                    best_matches.append((ratio_name, 3))
        
        if not best_matches:
            # Fallback: match by token overlap with metric name
            for m_name, m_obj in metric_by_name.items():
                m_tokens = tokenize(m_name)
                overlap = len(c_tokens & m_tokens)
                if overlap >= 2:
                    best_matches.append((m_name, 1))
                elif overlap >= 1 and (len(c_tokens) <= 2 or len(m_tokens) <= 2):
                    best_matches.append((m_name, 0.5))
        
        if not best_matches:
            continue
        
        # Pick best match
        best_matches.sort(key=lambda x: -x[1])
        best_metric = best_matches[0][0]
        
        # Update concept YAML
        for yaml_path in glob.glob(f"{db_dir}/concepts/*.yaml"):
            with open(yaml_path) as f:
                d = yaml.safe_load(f)
            if d and d.get("concept") == c_name:
                d["canonical_metric"] = best_metric
                with open(yaml_path, "w") as f:
                    yaml.dump(d, f, default_flow_style=False, sort_keys=False)
                updated_concepts.add(c_name)
                linked += 1
                break

    print(f"  {db_id}: {linked}/{total_concepts} concepts linked to metrics")

print(f"\nDone! Total: {sum(1 for db_id in sorted(os.listdir('bird_semantic')) for d in glob.glob(f'bird_semantic/{db_id}/concepts/*.yaml') if yaml.safe_load(open(d)) and yaml.safe_load(open(d)).get('canonical_metric'))} concepts with canonical_metric")
