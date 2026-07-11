"""Debug mock router failures."""
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "semantic_modeling" / "src"))
sys.path.insert(0, str(Path.home() / "enterprise-nl2sql"))

from semantic_engine.compiler.model_compiler import SemanticModelCompiler
from semantic_engine.loader.yaml_loader import load_semantic_model_file
from src.semantic_registry.pipeline.semantic_router import RouterResult, compile_from_router

snapshot = SemanticModelCompiler().compile(
    load_semantic_model_file(Path.home() / "enterprise-nl2sql/bird_semantic_engine/debit_card_specializing/model.yml")
)

with open(Path.home() / "enterprise-nl2sql/bird_bench/dev/dev_20240627/dev.json") as f:
    data = json.load(f)

questions = [d for d in data if d["db_id"] == "debit_card_specializing"]
db_path = Path.home() / "enterprise-nl2sql/bird_bench/dev/dev_20240627/databases/dev_databases/debit_card_specializing/debit_card_specializing.sqlite"


def find_measure_for_gold(sql, snapshot):
    upper = sql.upper()
    agg_match = re.search(r"(SUM|COUNT|AVG|MAX|MIN)\s*\(\s*(\w+)\.(\w+)", upper)
    if not agg_match:
        return None, "no aggregation"
    agg_func = agg_match.group(1)
    col_name = agg_match.group(3).lower()
    
    candidates = []
    for entity in snapshot.entities.values():
        for measure in entity.measures:
            measure_col = str(measure.expr).lower() if measure.expr else ""
            measure_col_short = measure_col.split(".")[-1].strip()
            if col_name in measure_col or col_name == measure_col_short:
                if measure.aggregation and measure.aggregation.upper() == agg_func:
                    return f"{entity.name}.{measure.name}", "exact"
                candidates.append((f"{entity.name}.{measure.name}", measure.aggregation))
    if candidates:
        return candidates[0][0], f"agg_mismatch (wanted {agg_func}, got {candidates[0][1]})"
    return None, f"no measure for column {col_name}"


def extract_filters_from_gold(sql, snapshot):
    """Extract filters from gold SQL WHERE clause using dimension/identifier names."""
    filters = []
    where_match = re.search(r"WHERE\s+(.+?)(?:\s+ORDER\s|\s+LIMIT\s|\s+GROUP\s|$)", sql, re.IGNORECASE | re.DOTALL)
    if not where_match:
        return filters, "no WHERE"
    
    col_to_member = {}
    for entity in snapshot.entities.values():
        for dim in entity.dimensions:
            col_raw = str(dim.expr) if dim.expr else ""
            col = col_raw.split(".")[-1].strip()
            col_to_member[col.lower()] = (f"{entity.name}.{dim.name}", "dimension")
        for ident in entity.identifiers:
            col_raw = str(ident.expr) if ident.expr else ""
            col = col_raw.split(".")[-1].strip()
            col_to_member[col.lower()] = (f"{entity.name}.{ident.name}", "identifier")
    
    unmatched = []
    where_clause = where_match.group(1).strip()
    conditions = re.split(r"\s+AND\s+", where_clause)
    for cond in conditions:
        cond = cond.strip()
        eq = re.match(r"(\w+)\.(\w+)\s*=\s*'([^']+)'", cond, re.IGNORECASE)
        if eq and eq.group(2).lower() in col_to_member:
            member, _ = col_to_member[eq.group(2).lower()]
            filters.append({"member": member, "operator": "equals", "values": [eq.group(3)]})
            continue
        like = re.match(r"(\w+)\.(\w+)\s+LIKE\s+'([^']+)'", cond, re.IGNORECASE)
        if like and like.group(2).lower() in col_to_member:
            member, _ = col_to_member[like.group(2).lower()]
            filters.append({"member": member, "operator": "like", "values": [like.group(3)]})
            continue
        gt = re.match(r"(\w+)\.(\w+)\s*>\s*'?([^']+)'?", cond, re.IGNORECASE)
        if gt and gt.group(2).lower() in col_to_member:
            member, _ = col_to_member[gt.group(2).lower()]
            filters.append({"member": member, "operator": "gt", "values": [gt.group(3).strip("'\"")]})
            continue
        unmatched.append(cond[:50])
    
    return filters, ", ".join(unmatched) if unmatched else "matched"


conn = sqlite3.connect(str(db_path))

for q in questions:
    sql = q["SQL"]
    upper = sql.upper()
    
    # Skip non-aggregations
    if not re.search(r"(SUM|COUNT|AVG|MAX|MIN)\s*\(", upper):
        continue
    
    # Skip subqueries 
    if "SELECT" in upper[upper.index("FROM"):] if "FROM" in upper else False:
        if "INNER JOIN" not in upper:
            # Might have subquery
            continue
    
    measure_name, reason = find_measure_for_gold(sql, snapshot)
    if not measure_name:
        continue
    
    filters, filter_reason = extract_filters_from_gold(sql, snapshot)
    
    result = RouterResult(measure=measure_name, dimensions=[], time_dimension=None, granularity=None, filters=filters, confidence=0.95)
    compiled = compile_from_router(snapshot, result, q["question"])
    
    status = "ok"
    error = ""
    if compiled is None:
        status = "compile_fail"
    else:
        try:
            cursor = conn.execute(compiled.sql, compiled.parameters)
            rows = cursor.fetchall()
        except Exception as e:
            status = "exec_fail"
            error = str(e)[:100]
    
    print(f"[{q['question_id']:>4}] {status:<12} measure={measure_name:<30} reason={reason:<20} filters={filter_reason[:30]:<30}")
    if compiled and "exec_fail" in status:
        print(f"      SQL: {compiled.sql}")
        print(f"      Params: {compiled.parameters}")
        print(f"      Error: {error}")
    
conn.close()
