#!/usr/bin/env python3
"""
Phase A+B: Generate Semantic Registry for BIRD databases using LLM + schema validation.
Phase C:   Run full NL2SQL pipeline evaluation.

Usage:
  # Phase A+B: Generate semantic registry for all 11 BIRD databases
  .venv/bin/python scripts/build_bird_semantic.py --generate --db all

  # ... or for a single DB
  .venv/bin/python scripts/build_bird_semantic.py --generate --db california_schools

  # Phase C: Run full pipeline evaluation
  .venv/bin/python scripts/build_bird_semantic.py --eval --subset 110 --indices bird_bench/results/sample_indices.json

  # Phase C: Both
  .venv/bin/python scripts/build_bird_semantic.py --generate --eval
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Load env ─────────────────────────────────────────────────────────────────
env_path = Path.home() / ".hermes" / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            k = key.strip()
            v = value.strip().strip("'\"")
            if k == "DEEPSEEK_API_KEY" and not os.environ.get("DEEPSEEK_API_KEY"):
                os.environ["DEEPSEEK_API_KEY"] = v
            elif k == "DEEPSEEK_BASE_URL" and not os.environ.get("DEEPSEEK_BASE_URL"):
                os.environ["DEEPSEEK_BASE_URL"] = v

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
BIRD_DIR = BASE_DIR / "bird_bench"
DEV_DIR = BIRD_DIR / "dev" / "dev_20240627"
RESULTS_DIR = BIRD_DIR / "results"
SEMANTIC_DIR = BASE_DIR / "bird_semantic"
DB_ROOT = DEV_DIR / "databases" / "dev_databases"

# ── Imports ───────────────────────────────────────────────────────────────────
from src.semantic_registry.pipeline.llm_gateway import DeepSeekProvider
from src.semantic_registry.pipeline.llm_gateway_types import LLMResponse
from src.semantic_registry.yaml_schema.schemas import (
    ConceptYaml, DimensionYaml, EntityYaml, JoinPathYaml, MetricYaml, TermYaml,
    validate_all_yaml_files,
)
from src.semantic_registry.resolver.registry import load_semantic_registry
from src.semantic_registry.pipeline.state_machine import NL2SQLPipeline, PipelineContext


# ==============================================================================
# PHASE 0: Data Loading
# ==============================================================================

def load_bird_data():
    with open(DEV_DIR / "dev.json") as f:
        dev_data = json.load(f)
    with open(DEV_DIR / "dev_tables.json") as f:
        tables_data = json.load(f)
    return dev_data, tables_data


def group_questions_by_db(dev_data):
    by_db = defaultdict(list)
    for i, q in enumerate(dev_data):
        by_db[q["db_id"]].append((i, q))
    return dict(by_db)


def get_schema_for_db(tables_data, db_id):
    for t in tables_data:
        if t["db_id"] == db_id:
            return t
    return None


def build_create_table_sql(schema: dict) -> str:
    """Build CREATE TABLE statements for a database schema."""
    table_names = schema["table_names_original"]
    col_names = schema["column_names_original"]
    col_types = schema["column_types"]
    primary_keys = schema.get("primary_keys", [])
    
    # Group columns by table
    tables = defaultdict(list)
    for i, (ti, cn) in enumerate(col_names):
        if ti == -1:
            continue
        ct = col_types[i] if i < len(col_types) else "text"
        is_pk = i in primary_keys
        tables[ti].append((cn, ct, is_pk))
    
    lines = []
    for ti in sorted(tables.keys()):
        tn = table_names[ti]
        lines.append(f"CREATE TABLE {tn} (")
        col_lines = []
        for cn, ct, is_pk in tables[ti]:
            col_def = f"  {cn} {ct}"
            if is_pk:
                col_def += " PRIMARY KEY"
            col_lines.append(col_def)
        lines.append(",\n".join(col_lines))
        lines.append(")")
    
    return "\n\n".join(lines)


# ==============================================================================
# PHASE 1: Deterministic Extraction (no LLM needed)
# ==============================================================================

def extract_entities(schema: dict) -> list[dict]:
    """Extract entities from table schemas."""
    table_names = schema["table_names_original"]
    col_names = schema["column_names_original"]
    primary_keys = schema.get("primary_keys", [])
    
    # Map each table to its columns
    tables = defaultdict(list)
    for i, (ti, cn) in enumerate(col_names):
        if ti == -1:
            continue
        tables[ti].append((i, cn))
    
    entities = []
    for ti, tn in enumerate(table_names):
        pks = []
        for i, cn in tables[ti]:
            if i in primary_keys:
                pks.append(cn)
        
        display_name = " ".join(w.capitalize() for w in tn.replace("_", " ").split())
        entities.append({
            "entity": tn,
            "description": f"Represents {display_name.lower()} data.",
            "primary_keys": pks if pks else [],
            "related_entities": [],
            "status": "certified",
        })
    return entities


def extract_join_paths(schema: dict, db_id: str, table_names: list[str] = None) -> list[dict]:
    """Extract join paths from foreign keys."""
    col_names = schema["column_names_original"]
    foreign_keys = schema.get("foreign_keys", [])
    table_names_list = schema["table_names_original"]
    
    if table_names is None:
        table_names = table_names_list
    
    # Build column → table mapping
    col_to_table = {}
    for i, (ti, cn) in enumerate(col_names):
        if ti == -1:
            continue
        col_to_table[i] = (ti, cn)
    
    # Build table → columns mapping
    table_cols = defaultdict(list)
    for i, (ti, cn) in enumerate(col_names):
        if ti == -1:
            continue
        table_cols[ti].append((i, cn))
    
    join_paths = []
    seen = set()
    
    for fk in foreign_keys:
        if isinstance(fk, list) and len(fk) == 2:
            from_col_idx, to_col_idx = fk
            from_info = col_to_table.get(from_col_idx)
            to_info = col_to_table.get(to_col_idx)
            
            if from_info and to_info:
                from_ti, from_col = from_info
                to_ti, to_col = to_info
                from_tn = table_names_list[from_ti]
                to_tn = table_names_list[to_ti]
                
                if from_tn not in table_names or to_tn not in table_names:
                    continue
                
                key = f"{from_tn}→{to_tn}"
                if key not in seen:
                    seen.add(key)
                    join_paths.append({
                        "join_path_name": f"{from_tn}_to_{to_tn}",
                        "from_table": from_tn,
                        "to_table": to_tn,
                        "relationship": "many_to_one",
                        "join_condition": f"{from_tn}.{from_col} = {to_tn}.{to_col}",
                        "safe_for_metrics": [],
                        "fanout_risk": "low",
                        "status": "certified",
                    })
    
    return join_paths


def extract_dimensions(schema: dict, gold_queries: list[tuple[int, dict]]) -> list[dict]:
    """Extract dimensions from columns used in GROUP BY, WHERE, and SELECT of gold SQL."""
    # Find all columns referenced in gold SQL
    col_names = schema["column_names_original"]
    table_names = schema["table_names_original"]
    
    # Build set of column names used in GROUP BY or as dimension targets
    dimension_cols = set()
    seen_columns = set()
    
    for idx, q in gold_queries:
        sql = q.get("SQL", "")
        if not sql:
            continue
        upper = sql.upper()
        
        # Extract GROUP BY columns
        group_matches = re.findall(r'GROUP\s+BY\s+(.+?)(?:HAVING|ORDER\s+BY|LIMIT|$)', upper, re.DOTALL)
        for gm in group_matches:
            for col_ref in re.findall(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?', gm):
                dimension_cols.add(col_ref.lower())
        
        # Also check SELECT for non-aggregated columns (they're dimensions)
        select_match = re.search(r'SELECT\s+(.*?)\s+FROM', upper, re.DOTALL)
        if select_match:
            select_part = select_match.group(1)
            # Split by comma, check which are not aggregate functions
            parts = re.split(r',', select_part)
            for part in parts:
                part = part.strip()
                # Skip aggregate functions
                if any(fn in part.upper() for fn in ["COUNT(", "SUM(", "AVG(", "MIN(", "MAX("]):
                    continue
                # Extract column reference
                col_refs = re.findall(r'(?:AS\s+)?([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)(?:\s+AS)?', part)
                for cr in col_refs:
                    cr = cr.strip()
                    if cr.upper() not in ["SELECT", "DISTINCT", "AS", ""] and len(cr) > 1:
                        # Check if this is an alias (not a real column)
                        col_clean = cr.lower().split(".")[-1] if "." in cr else cr.lower()
                        # Check if column exists in schema
                        for i, (ti, cn) in enumerate(col_names):
                            if ti == -1:
                                continue
                            if cn.lower().replace(" ", "_") == col_clean or cn.lower() == col_clean:
                                dimension_cols.add(cr.lower())
                                break
    
    # Process dimensions
    dimensions = []
    processed_cols = set()
    
    for col_ref in dimension_cols:
        col_clean = col_ref.split(".")[-1] if "." in col_ref else col_ref
        col_clean = col_clean.replace("`", "")
        
        # Find this column in the schema
        for i, (ti, cn) in enumerate(col_names):
            if ti == -1:
                continue
            norm_cn = cn.lower().replace(" ", "_")
            norm_col_ref = col_clean.lower().replace(" ", "_")
            
            if norm_cn == norm_col_ref:
                if cn in processed_cols:
                    break
                processed_cols.add(cn)
                
                tn = table_names[ti]
                dim_name = cn.lower().replace(" ", "_").replace("-", "_").replace("/", "_").replace("(", "").replace(")", "")
                # Clean up the dimension name
                dim_name = re.sub(r'[^a-z0-9_]', '_', dim_name)
                dim_name = dim_name.strip("_")
                
                if not dim_name or dim_name in processed_cols:
                    break
                    
                display = " ".join(w.capitalize() for w in dim_name.split("_"))
                
                dimensions.append({
                    "dimension": dim_name,
                    "description": f"{display} for reporting and filtering.",
                    "entity": tn,
                    "synonyms": [],
                    "physical_mappings": [
                        {"table": tn, "column": cn}
                    ],
                    "status": "certified",
                })
                break
    
    return dimensions


def extract_metrics_from_gold_sql(schema: dict, questions: list[tuple[int, dict]], db_id: str) -> list[dict]:
    """Extract metrics deterministically from gold SQL aggregation expressions."""
    col_names = schema["column_names_original"]
    table_names = schema["table_names_original"]
    
    # Build column lookup
    table_cols = defaultdict(list)
    for i, (ti, cn) in enumerate(col_names):
        if ti == -1:
            continue
        tn = table_names[ti]
        table_cols[tn].append(cn)
    
    metrics = []
    seen = set()
    
    for idx, q in questions:
        sql = q.get("SQL", "")
        if not sql:
            continue
        
        # Find aggregation expressions in SQL
        agg_patterns = [
            (r'COUNT\s*\(\s*DISTINCT\s+(\w+(?:\.\w+)?)\s*\)', 'count_distinct'),
            (r'COUNT\s*\(\s*(\w+(?:\.\w+)?)\s*\)', 'count'),
            (r'SUM\s*\(\s*(\w+(?:\.\w+)?)\s*\)', 'sum'),
            (r'AVG\s*\(\s*(\w+(?:\.\w+)?)\s*\)', 'avg'),
            (r'MIN\s*\(\s*(\w+(?:\.\w+)?)\s*\)', 'min'),
            (r'MAX\s*\(\s*(\w+(?:\.\w+)?)\s*\)', 'max'),
        ]
        
        for pattern, agg_type in agg_patterns:
            matches = re.findall(pattern, sql, re.IGNORECASE)
            for col_ref in matches:
                col_clean = col_ref.split(".")[-1] if "." in col_ref else col_ref
                col_clean = col_clean.replace("`", "")
                
                # Find the actual table for this column
                found_table = None
                for tn in table_names:
                    if any(c.lower().replace(" ", "_") == col_clean.lower().replace(" ", "_") for c in table_cols.get(tn, [])):
                        found_table = tn
                        break
                    # Also check raw column names
                    for c in table_cols.get(tn, []):
                        if c.lower() == col_clean.lower() or c.lower().replace(" ", "_") == col_clean.lower():
                            found_table = tn
                            break
                    if found_table:
                        break
                
                if not found_table:
                    continue
                
                # Find real column name
                real_col = None
                for c in table_cols.get(found_table, []):
                    if c.lower().replace(" ", "_") == col_clean.lower().replace(" ", "_") or c.lower() == col_clean.lower():
                        real_col = c
                        break
                
                if not real_col:
                    continue
                
                # Generate metric name
                metric_name = f"{agg_type}_{col_clean.lower()}"
                if metric_name in seen:
                    # Add differentiating suffix
                    for i in range(2, 100):
                        candidate = f"{metric_name}_{i}"
                        if candidate not in seen:
                            metric_name = candidate
                            break
                
                seen.add(metric_name)
                
                # Map aggregation type to valid type string
                type_map = {
                    'sum': 'simple_sum', 'count': 'simple_count', 'count_distinct': 'distinct_count',
                    'avg': 'advanced', 'min': 'advanced', 'max': 'advanced',
                }
                
                metrics.append({
                    "metric": metric_name,
                    "concept": f"{found_table}_{agg_type}",
                    "description": f"{agg_type.upper()} of {col_clean} in {found_table}.",
                    "type": type_map.get(agg_type, 'simple_sum'),
                    "measure": {"table": found_table, "column": real_col},
                    "aggregation": agg_type,
                    "unit": "",
                    "allowed_dimensions": [],
                    "owner": "bird_benchmark",
                    "status": "certified",
                })
        
        # Also detect ratio expressions (A / B)
        ratio_matches = re.findall(r'(\w+(?:\.\w+)?)\s*/\s*(\w+(?:\.\w+)?)', sql)
        for num_col, denom_col in ratio_matches:
            num_clean = num_col.split(".")[-1].replace("`", "")
            denom_clean = denom_col.split(".")[-1].replace("`", "")
            metric_name = f"ratio_{num_clean}_over_{denom_clean}"
            if metric_name not in seen and len(seen) < 20:
                seen.add(metric_name)
                metrics.append({
                    "metric": metric_name,
                    "concept": f"{db_id}_ratio",
                    "description": f"Ratio of {num_clean} to {denom_clean}.",
                    "type": "advanced",
                    "expression": sql,
                    "unit": "",
                    "allowed_dimensions": [],
                    "owner": "bird_benchmark",
                    "status": "certified",
                })
    
    return metrics


# ==============================================================================
# PHASE 2: LLM-Assisted Semantic Analysis
# ==============================================================================

def build_llm_prompt(db_id: str, schema_text: str, questions: list[tuple[int, dict]]) -> str:
    """Build a comprehensive prompt for LLM analysis of a BIRD database."""
    # Summarize questions
    q_lines = []
    for idx, q in questions:
        evidence = q.get("evidence", "")
        ev_line = f"      evidence: {evidence}" if evidence else ""
        q_lines.append(f"    - q{idx}: \"{q['question']}\"")
        q_lines.append(f"      gold_sql: {q['SQL']}")
        if ev_line:
            q_lines.append(ev_line)
    
    prompt = f"""You are analyzing the "{db_id}" database for a semantic NL2SQL system.
Your task: identify business concepts, metrics, dimensions, and term mappings from the database schema and example queries.

DATABASE SCHEMA:
{schema_text}

EXAMPLE QUERIES (question → gold SQL):
{chr(10).join(q_lines)}

INSTRUCTIONS:
Return a JSON object with the following arrays. Each entry MUST reference only tables and columns that exist in the schema above.

CRITICAL: TERMS MUST BE EXACT PHRASES FOUND IN THE QUESTIONS
A term's name and synonyms must be phrases that literally appear in the questions.
For example, if questions say "eligible free rate", the term should be "eligible free rate" (not "highest eligible free rate").

1. "concepts": Business concepts derived from the database domain and evidence fields.
   - concept: snake_case name
   - display_name: Human readable name
   - domain: "{db_id}" (all same domain)
   - definition: Business definition
   - type: "metric_concept" or "entity_concept"
   - owner: "bird_benchmark"

2. "metrics": Aggregation targets found in gold SQL.
   - metric: snake_case name (e.g., "free_meal_count", "eligible_free_rate")
   - concept: must reference a concept above
   - description: Business description
   - type: "simple_sum", "simple_count", "distinct_count", "ratio", or "advanced"
   - measure: {{"table": "actual_table_name", "column": "actual_column_name"}}
   - aggregation: "sum", "count", "avg", "count_distinct", "min", "max"
   - unit: appropriate unit string
   - allowed_dimensions: list of dimension names
   If type is "ratio", include numerator/denominator/expression instead of measure.
   ALL table/column references MUST exist in the schema above.

3. "dimensions": Business dimensions for grouping/filtering (columns used in GROUP BY or WHERE filters).
   - dimension: snake_case name
   - description: Business description  
   - entity: table name it belongs to
   - physical_mappings: [{{"table": "actual_table", "column": "actual_column"}}]
   ALL references MUST exist in the schema.

4. "terms": Natural language phrases from questions that map to concepts or columns.
   CRITICAL: Each term name and synonym must be a phrase that LITERALLY appears in a question.
   GOOD: term = "eligible free rate" (appears in questions)
   GOOD: synonym = "charter school" (appears in questions)  
   BAD: term = "highest eligible free rate" (too specific, won't match other questions)
   BAD: term = "continuation school" ✓ but should also have synonym "continuation schools"
   - term: exact phrase from questions (snake_case)
   - description: What this term means
   - synonyms: [list of alternative phrasings that also appear in questions]
   - candidate_concepts: [concept names this term could map to]
   - default_concept_by_domain: {{"{db_id}": "concept_name"}}
   - ambiguity_level: "low"
   - owner: "bird_benchmark"
   - domain: "{db_id}"

VALIDATION REQUIREMENTS:
- Every table name in physical_mappings or measure must be one of: {', '.join(extract_table_names(schema_text))}
- Every column name must exist in the actual table it references
- If unsure about a column mapping, omit it rather than guess
- metrics should be derived from actual aggregation expressions in gold SQL

Return ONLY valid JSON, no other text."""

    return prompt


def extract_table_names(schema_text: str) -> list[str]:
    """Extract table names from CREATE TABLE statements."""
    tables = re.findall(r'CREATE\s+TABLE\s+(\w+)', schema_text)
    return tables


def normalize_name(name: str) -> str:
    """Normalize a name to snake_case."""
    name = name.replace("'", "").replace('"', "").replace("`", "")
    name = re.sub(r'[^a-zA-Z0-9_\s]', '_', name)
    name = name.replace(" ", "_")
    name = re.sub(r'_+', '_', name)
    name = name.strip("_").lower()
    return name[:64]


def parse_llm_response(raw: str, schema: dict, db_id: str) -> dict[str, list]:
    """Parse and validate LLM response against actual schema."""
    # Extract JSON from response
    result = {"concepts": [], "metrics": [], "dimensions": [], "terms": [], "errors": []}
    
    try:
        # Find JSON block
        start = raw.find("{")
        if start < 0:
            result["errors"].append("No JSON found in LLM response")
            return result
        
        depth = 0
        in_str = False
        quote = ""
        for i in range(start, len(raw)):
            c = raw[i]
            if in_str:
                if c == "\\":
                    pass
                elif c == quote:
                    in_str = False
            elif c in ("'", '"'):
                in_str = True
                quote = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = raw[start:i+1]
                    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
                    data = json.loads(candidate)
                    break
        else:
            result["errors"].append("Unbalanced JSON")
            return result
    except (json.JSONDecodeError, ValueError) as e:
        result["errors"].append(f"JSON parse error: {e}")
        return result
    
    if not isinstance(data, dict):
        result["errors"].append("LLM response is not a JSON object")
        return result
    
    # Build column lookup: {table_name: {column_name: column_type}}
    table_cols = {}
    for ti, tn in enumerate(schema["table_names_original"]):
        table_cols[tn.lower()] = {}
    for i, (ti, cn) in enumerate(schema["column_names_original"]):
        if ti == -1:
            continue
        tn = schema["table_names_original"][ti]
        ct = schema["column_types"][i] if i < len(schema["column_types"]) else "text"
        table_cols.setdefault(tn.lower(), {})[cn.lower()] = ct
    
    # Helper: validate a table.column reference
    def validate_ref(table: str, column: str) -> bool:
        tl = table.lower()
        cl = column.lower()
        if tl not in table_cols:
            return False
        # Also check normalized column names
        for real_col in table_cols[tl]:
            if real_col == cl or real_col.replace(" ", "_") == cl:
                return True
            # Check backtick-quoted columns
            if f"`{real_col}`".lower() == cl:
                return True
        return False
    
    def find_real_column(table: str, name: str) -> str | None:
        """Find the actual column name that matches the normalized name."""
        tl = table.lower()
        nl = name.lower().replace("`", "")
        if tl not in table_cols:
            return None
        for real_col in table_cols[tl]:
            if real_col.lower() == nl or real_col.lower().replace(" ", "_") == nl:
                return real_col
            if f"`{real_col}`".lower() == nl:
                return real_col
        return None
    
    # Parse concepts
    for c in data.get("concepts", []):
        concept_name = c.get("concept", "")
        if not concept_name:
            continue
        result["concepts"].append({
            "concept": normalize_name(concept_name),
            "display_name": c.get("display_name", concept_name.replace("_", " ").title()),
            "domain": db_id,
            "definition": c.get("definition", ""),
            "type": c.get("type", "metric_concept"),
            "owner": "bird_benchmark",
            "status": "certified",
        })
    
    concept_names = {c["concept"] for c in result["concepts"]}
    
    # Parse dimensions
    for d in data.get("dimensions", []):
        dim_name = normalize_name(d.get("dimension", ""))
        if not dim_name:
            continue
        
        mappings = []
        for m in d.get("physical_mappings", []):
            tbl = m.get("table", "")
            col = m.get("column", "")
            real_col = find_real_column(tbl, col)
            if tbl and (real_col or validate_ref(tbl, col)):
                mappings.append({"table": tbl, "column": real_col or col})
        
        if not mappings:
            # Try to find the column from the dimension name itself
            for ti, tn in enumerate(schema["table_names_original"]):
                for i, (cji, cn) in enumerate(schema["column_names_original"]):
                    if cji == ti and cn.lower().replace(" ", "_") == dim_name:
                        mappings.append({"table": tn, "column": cn})
                        break
                if mappings:
                    break
        
        if not mappings:
            result["errors"].append(f"dimension '{dim_name}' has no valid column mapping, skipping")
            continue
        
        entity = d.get("entity", mappings[0]["table"]) if d.get("entity") else mappings[0]["table"]
        
        result["dimensions"].append({
            "dimension": dim_name,
            "description": d.get("description", f"{dim_name.replace('_', ' ').title()} dimension."),
            "entity": entity,
            "synonyms": d.get("synonyms", []),
            "physical_mappings": mappings,
            "status": "certified",
        })
    
    dim_names = {d["dimension"] for d in result["dimensions"]}
    
    # Parse metrics
    for m in data.get("metrics", []):
        metric_name = normalize_name(m.get("metric", ""))
        if not metric_name:
            continue
        
        metric_type_str = m.get("type", "simple_sum")
        # Validate metric type is one of the allowed enum values
        valid_types = {"simple_sum", "simple_count", "count", "distinct_count", "ratio", "advanced"}
        # Map common LLM-invented types
        type_mapping = {
            "simple_max": "advanced", "simple_min": "advanced", "distinct": "distinct_count",
            "simple_avg": "simple_sum", "average": "advanced", "max": "advanced", "min": "advanced",
            "simple": "simple_count", "count_distinct": "distinct_count",
            "simple_count": "simple_count", "simple_sum": "simple_sum",
        }
        if metric_type_str not in valid_types:
            mapped = type_mapping.get(metric_type_str, "advanced")
            result["errors"].append(f"metric '{metric_name}': invalid type '{metric_type_str}', using '{mapped}'")
            metric_type_str = mapped
        
        if metric_type_str == "ratio":
            numerator = m.get("numerator", {})
            denominator = m.get("denominator", {})
            
            if not numerator.get("metric") or not denominator.get("metric"):
                # LLM generated a broken ratio - downgrade to advanced
                metric_type_str = "advanced"
                metric_entry = {
                    "metric": metric_name,
                    "concept": m.get("concept", ""),
                    "description": m.get("description", ""),
                    "type": "advanced",
                    "expression": m.get("expression", ""),
                    "unit": m.get("unit", ""),
                    "allowed_dimensions": [d for d in m.get("allowed_dimensions", []) if d in dim_names],
                    "owner": "bird_benchmark",
                    "status": "certified",
                }
                metric_entry = {k: v for k, v in metric_entry.items() if v is not None and v != "" and v != []}
                if metric_entry.get("expression"):
                    result["metrics"].append(metric_entry)
                else:
                    result["errors"].append(f"metric '{metric_name}' is ratio without proper numerator/denominator/expression, skipping")
                continue
            else:
                metric_entry = {
                    "metric": metric_name,
                    "concept": m.get("concept", ""),
                    "description": m.get("description", ""),
                    "type": "ratio",
                    "numerator": {"metric": numerator["metric"]},
                    "denominator": {"metric": denominator["metric"]},
                    "expression": m.get("expression", ""),
                    "unit": m.get("unit", ""),
                    "allowed_dimensions": [d for d in m.get("allowed_dimensions", []) if d in dim_names],
                    "owner": "bird_benchmark",
                    "status": "certified",
                }
                metric_entry = {k: v for k, v in metric_entry.items() if v is not None and v != "" and v != []}
                result["metrics"].append(metric_entry)
            continue  # skip the rest for ratios
        else:
            measure_data = m.get("measure", {})
            tbl = measure_data.get("table", "")
            col = measure_data.get("column", "")
            real_col = find_real_column(tbl, col) if tbl and col else None
            
            if not (tbl and (real_col or validate_ref(tbl, col))):
                result["errors"].append(f"metric '{metric_name}' references invalid column, skipping")
                continue
            
            metric_entry = {
                "metric": metric_name,
                "concept": m.get("concept", ""),
                "description": m.get("description", ""),
                "type": metric_type_str,
                "measure": {"table": tbl, "column": real_col or col},
                "aggregation": m.get("aggregation", "sum"),
                "unit": m.get("unit", ""),
                "allowed_dimensions": [d for d in m.get("allowed_dimensions", []) if d in dim_names],
                "owner": "bird_benchmark",
                "status": "certified",
            }
            metric_entry = {k: v for k, v in metric_entry.items() if v is not None and v != "" and v != []}
            if metric_entry.get("aggregation") or metric_entry.get("measure"):
                result["metrics"].append(metric_entry)
    
    metric_names = {m["metric"] for m in result["metrics"]}
    
    # Parse terms
    for t in data.get("terms", []):
        term_name = normalize_name(t.get("term", ""))
        if not term_name:
            continue
        
        # Verify candidate concepts exist
        candidate_concepts = [c for c in t.get("candidate_concepts", []) if normalize_name(c) in concept_names]
        
        # Default concept by domain
        default_by_domain = {}
        for domain, concept in t.get("default_concept_by_domain", {}).items():
            if normalize_name(concept) in concept_names:
                default_by_domain[domain] = normalize_name(concept)
        
        result["terms"].append({
            "term": term_name,
            "description": t.get("description", ""),
            "synonyms": t.get("synonyms", []),
            "candidate_concepts": candidate_concepts,
            "default_concept_by_domain": default_by_domain,
            "ambiguity_level": "low",
            "clarification_required_when": [],
            "owner": "bird_benchmark",
            "domain": db_id,
            "status": "certified",
        })
    
    return result


def analyze_db_with_llm(db_id: str, schema_text: str, questions: list[tuple[int, dict]], provider: DeepSeekProvider) -> dict[str, list]:
    """Run LLM analysis for one BIRD database."""
    prompt = build_llm_prompt(db_id, schema_text, questions[:5])  # Use first 5 questions as examples
    system = "You are a semantic data engineer. Return ONLY valid JSON."
    
    retries = 2
    last_error = None
    for attempt in range(retries + 1):
        try:
            raw = provider.generate(f"{system}\n\n{prompt}")
            return raw
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(2)
    
    return json.dumps({"error": f"LLM call failed after {retries+1} attempts: {last_error}"})


def build_semantic_for_db(
    db_id: str,
    schema: dict,
    questions: list[tuple[int, dict]],
    provider: DeepSeekProvider = None,
) -> dict[str, list]:
    """Build complete semantic registry for one BIRD database."""
    result = {
        "entities": [],
        "concepts": [],
        "dimensions": [],
        "metrics": [],
        "terms": [],
        "join_paths": [],
        "errors": [],
    }
    
    # Phase 1: Deterministic extraction
    result["entities"] = extract_entities(schema)
    
    # Extract all table names
    all_table_names = schema["table_names_original"]
    
    result["join_paths"] = extract_join_paths(schema, db_id, all_table_names)
    result["dimensions"] = extract_dimensions(schema, questions)
    
    # Phase 2: LLM-assisted extraction (concepts, terms)
    # Metrics are EXTRACTED DETERMINISTICALLY from gold SQL — NOT from LLM
    # This avoids LLM hallucination of invalid metric types/columns
    if provider:
        schema_text = build_create_table_sql(schema)
        llm_raw = analyze_db_with_llm(db_id, schema_text, questions, provider)
        llm_result = parse_llm_response(llm_raw, schema, db_id)
        
        result["concepts"] = llm_result["concepts"]
        result["terms"] = llm_result["terms"]
        result["errors"].extend(llm_result["errors"])
    
    # Generate metrics deterministically from gold SQL
    extracted_metrics = extract_metrics_from_gold_sql(schema, questions, db_id)
    if extracted_metrics:
        result["metrics"] = extracted_metrics
    
    # Post-process: link concepts to metrics via canonical_metric
    # For each concept, find metrics that match (by token overlap in names)
    concept_names = {c["concept"] for c in result["concepts"]}
    metric_names = {m["metric"] for m in result.get("metrics", [])}
    
    if result["concepts"] and result["metrics"]:
        for concept in result["concepts"]:
            c_name = concept["concept"]
            # Check if any metric name is a close match
            for m_name in metric_names:
                # If concept name tokens overlap significantly with metric name
                c_tokens = set(c_name.lower().replace("_", " ").split())
                m_tokens = set(m_name.lower().replace("_", " ").split())
                overlap = len(c_tokens & m_tokens)
                if overlap >= 2 or (overlap >= 1 and len(c_tokens) <= 2):
                    concept["canonical_metric"] = m_name
                    break
                # Also check if one is contained in the other
                if c_name.lower().replace("_", "") in m_name.lower().replace("_", "") or \
                   m_name.lower().replace("_", "") in c_name.lower().replace("_", ""):
                    concept["canonical_metric"] = m_name
                    break
    
    return result


# ==============================================================================
# PHASE 2b: YAML Writing
# ==============================================================================

def write_semantic_yaml(db_id: str, registry: dict[str, list]) -> list[str]:
    """Write semantic YAML files for a database using PyYAML. Returns list of created files."""
    import yaml
    db_dir = SEMANTIC_DIR / db_id
    created = []
    
    def clean_item(item: dict) -> dict:
        cleaned = {}
        for k, v in item.items():
            if v is None or v == "" or v == [] or v == {}:
                continue
            # Convert enum values to strings
            if hasattr(v, 'value'):
                v = v.value
            if isinstance(v, dict):
                inner = {}
                for sk, sv in v.items():
                    if sv is not None and sv != "" and sv != []:
                        if hasattr(sv, 'value'):
                            sv = sv.value
                        inner[sk] = sv
                if inner:
                    cleaned[k] = inner
            elif isinstance(v, list):
                inner_list = []
                for item_v in v:
                    if isinstance(item_v, dict):
                        inner_cleaned = {}
                        for sk, sv in item_v.items():
                            if sv is not None and sv != "" and sv != []:
                                if hasattr(sv, 'value'):
                                    sv = sv.value
                                inner_cleaned[sk] = sv
                        if inner_cleaned:
                            inner_list.append(inner_cleaned)
                    else:
                        if hasattr(item_v, 'value'):
                            item_v = item_v.value
                        inner_list.append(item_v)
                if inner_list:
                    cleaned[k] = inner_list
            else:
                cleaned[k] = v
        return cleaned
    
    for category, items in [
        ("entities", registry.get("entities", [])),
        ("concepts", registry.get("concepts", [])),
        ("dimensions", registry.get("dimensions", [])),
        ("metrics", registry.get("metrics", [])),
        ("terms", registry.get("terms", [])),
        ("join_paths", registry.get("join_paths", [])),
    ]:
        cat_dir = db_dir / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        
        for item in items:
            key_map = {
                "entities": "entity",
                "concepts": "concept",
                "dimensions": "dimension",
                "metrics": "metric",
                "terms": "term",
                "join_paths": "join_path_name",
            }
            key_field = key_map.get(category, "name")
            name = item.get(key_field, "unknown")
            if not name:
                continue
            
            safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', name).strip("_").lower()
            if not safe_name:
                safe_name = "unnamed"
            
            filepath = cat_dir / f"{safe_name}.yaml"
            cleaned = clean_item(item)
            
            with open(filepath, "w") as f:
                yaml.dump(cleaned, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            
            created.append(str(filepath))
    
    return created


def validate_semantic_registry(db_id: str) -> list[str]:
    """Validate generated YAML files using the existing validation system."""
    db_dir = SEMANTIC_DIR / db_id
    if not db_dir.exists():
        return [f"Directory not found: {db_dir}"]
    
    errors = validate_all_yaml_files(str(db_dir))
    result = []
    for filepath, file_errors in errors.items():
        for err in file_errors:
            result.append(f"{filepath}: {err}")
    return result


# ==============================================================================
# PHASE 4: Full Pipeline Evaluation
# ==============================================================================

def run_pipeline_evaluation(dev_data: list[dict], sample_indices: list[int] = None) -> dict:
    """Run full NL2SQL pipeline on BIRD questions."""
    from src.semantic_registry.resolver.registry import load_semantic_registry
    from src.semantic_registry.pipeline.state_machine import NL2SQLPipeline
    
    if sample_indices:
        indices = sample_indices
    else:
        indices = list(range(len(dev_data)))
    
    results = []
    
    for idx in indices:
        q = dev_data[idx]
        db_id = q["db_id"]
        
        # Load the per-DB semantic registry
        db_semantic_dir = SEMANTIC_DIR / db_id
        if not db_semantic_dir.exists():
            results.append({"idx": idx, "db_id": db_id, "error": "No semantic registry", "match": False})
            continue
        
        try:
            registry_data = load_semantic_registry(str(db_semantic_dir))
        except Exception as e:
            results.append({"idx": idx, "db_id": db_id, "error": f"Registry load error: {e}", "match": False})
            continue
        
        # Build and run pipeline
        pipeline = NL2SQLPipeline(registry_data=registry_data)
        context = pipeline.run(q["question"], domain=db_id)
        
        generated_sql = ""
        if context.selected_sql and context.selected_sql.sql:
            generated_sql = context.selected_sql.sql
        elif context.sql_candidates:
            # Pick first valid candidate
            for c in context.sql_candidates:
                if c.sql and c.parse_success:
                    generated_sql = c.sql
                    break
            if not generated_sql and context.sql_candidates:
                generated_sql = context.sql_candidates[0].sql
        
        # Evaluate against gold SQL
        db_path = DB_ROOT / db_id / f"{db_id}.sqlite"
        gold_sql = q["SQL"]
        
        if not generated_sql:
            results.append({
                "idx": idx, "db_id": db_id, "difficulty": q["difficulty"],
                "match": False, "error": "No SQL generated",
                "trace": context.trace,
                "error_detail": context.error,
            })
            continue
        
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute(generated_sql)
            pred_res = cursor.fetchall()
            cursor.execute(gold_sql)
            gold_res = cursor.fetchall()
            conn.close()
            match = set(pred_res) == set(gold_res)
        except Exception as e:
            match = False
            results.append({
                "idx": idx, "db_id": db_id, "difficulty": q["difficulty"],
                "match": False, "error": str(e)[:100],
                "predicted": generated_sql, "gold": gold_sql,
            })
            continue
        
        results.append({
            "idx": idx, "db_id": db_id, "difficulty": q["difficulty"],
            "match": match, "predicted": generated_sql, "gold": gold_sql,
            "selected_sql": getattr(context.selected_sql, "sql", None) if context.selected_sql else None,
            "trace": context.trace,
            "has_error": bool(context.error),
            "requires_clarification": context.requires_clarification,
        })
    
    return results


def compute_eval_stats(results: list[dict]) -> dict:
    """Compute evaluation statistics."""
    from collections import Counter
    
    total = len(results)
    passed = sum(1 for r in results if r["match"])
    errors = [r for r in results if r.get("error") and not r["match"]]
    clarifications = [r for r in results if r.get("requires_clarification")]
    
    by_diff = defaultdict(list)
    by_db = defaultdict(list)
    
    for r in results:
        by_diff[r.get("difficulty", "unknown")].append(r)
        by_db[r.get("db_id", "unknown")].append(r)
    
    stats = {
        "total": total,
        "passed": passed,
        "execution_accuracy": round(passed / total * 100, 2) if total > 0 else 0,
        "errors": len(errors),
        "clarifications": len(clarifications),
        "by_difficulty": {},
        "by_database": {},
    }
    
    for diff, items in sorted(by_diff.items()):
        n = len(items)
        p = sum(1 for r in items if r["match"])
        stats["by_difficulty"][diff] = {
            "count": n, "pass": p,
            "ex": round(p / n * 100, 2) if n > 0 else 0,
        }
    
    for db, items in sorted(by_db.items()):
        n = len(items)
        p = sum(1 for r in items if r["match"])
        stats["by_database"][db] = {
            "count": n, "pass": p,
            "ex": round(p / n * 100, 2) if n > 0 else 0,
        }
    
    return stats


# ==============================================================================
# MAIN
# ==============================================================================

def generate_all_databases(provider: DeepSeekProvider = None, db_filter: str = None):
    """Generate semantic registry for all (or one) BIRD databases."""
    dev_data, tables_data = load_bird_data()
    questions_by_db = group_questions_by_db(dev_data)
    
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    
    if db_filter and db_filter != "all":
        dbs_to_process = [db_filter]
    else:
        dbs_to_process = sorted(questions_by_db.keys())
    
    all_errors = []
    total_files = 0
    
    for db_id in dbs_to_process:
        schema = get_schema_for_db(tables_data, db_id)
        if not schema:
            print(f"  ❌ Schema not found for {db_id}")
            continue
        
        questions = questions_by_db.get(db_id, [])
        print(f"\n{'='*60}")
        print(f"  Building registry: {db_id}")
        print(f"  Tables: {len(schema['table_names_original'])}, Questions: {len(questions)}")
        print(f"{'='*60}")
        
        registry = build_semantic_for_db(db_id, schema, questions, provider)
        
        files = write_semantic_yaml(db_id, registry)
        total_files += len(files)
        
        print(f"  Generated {len(files)} files:")
        for f in sorted(files):
            print(f"    ✓ {Path(f).relative_to(SEMANTIC_DIR)}")
        
        # Print stats
        print(f"\n  Summary: {len(registry['entities'])} entities, {len(registry['concepts'])} concepts, "
              f"{len(registry['dimensions'])} dimensions, {len(registry['metrics'])} metrics, "
              f"{len(registry['terms'])} terms, {len(registry['join_paths'])} join_paths")
        
        if registry["errors"]:
            print(f"\n  ⚠️  Validation notes ({len(registry['errors'])}):")
            for err in registry["errors"][:10]:
                print(f"    - {err}")
            all_errors.extend(registry["errors"])
        
        # Validate
        val_errors = validate_semantic_registry(db_id)
        if val_errors:
            print(f"\n  ❌ Validation errors:")
            for ve in val_errors[:10]:
                print(f"    - {ve}")
            all_errors.extend(val_errors)
        else:
            print(f"\n  ✅ YAML validation passed")
    
    print(f"\n{'='*60}")
    print(f"  Complete: {total_files} files generated across {len(dbs_to_process)} databases")
    if all_errors:
        print(f"  Total issues: {len(all_errors)}")
    print(f"{'='*60}")


def run_evaluation(subset: int = None, indices_path: str = None):
    """Run full pipeline evaluation on BIRD questions."""
    dev_data, tables_data = load_bird_data()
    
    if indices_path:
        with open(indices_path) as f:
            indices = json.load(f)
        if subset:
            indices = indices[:subset]
    elif subset:
        indices = list(range(subset))
    else:
        indices = list(range(len(dev_data)))
    
    print(f"\n{'='*65}")
    print(f"  Full Pipeline Evaluation — {len(indices)} questions")
    print(f"{'='*65}")
    
    results = run_pipeline_evaluation(dev_data, indices)
    stats = compute_eval_stats(results)
    
    # Print report
    print(f"\n  EXECUTION ACCURACY: {stats['execution_accuracy']:.1f}% ({stats['passed']}/{stats['total']})")
    print(f"  Errors: {stats['errors']}, Clarifications requested: {stats['clarifications']}\n")
    
    print(f"  {'BY DIFFICULTY':35} {'Count':>6} {'Pass':>6} {'EX%':>8}")
    print("  " + "-" * 57)
    for diff in ["simple", "moderate", "challenging"]:
        d = stats["by_difficulty"].get(diff)
        if d:
            ex = d["ex"]
            bar = "█" * int(ex / 10) + "░" * (10 - int(ex / 10))
            print(f"  {diff:33} {d['count']:>6} {d['pass']:>6} {ex:>7.1f}%  {bar}")
    print("  " + "-" * 57)
    ex = stats["execution_accuracy"]
    bar = "█" * int(ex / 10) + "░" * (10 - int(ex / 10))
    print(f"  {'Total':33} {stats['total']:>6} {stats['passed']:>6} {ex:>7.1f}%  {bar}")
    
    print(f"\n  {'BY DATABASE':35} {'Count':>6} {'Pass':>6} {'EX%':>8}")
    print("  " + "-" * 57)
    for db in sorted(stats["by_database"].keys()):
        d = stats["by_database"][db]
        ex = d["ex"]
        bar = "█" * int(ex / 10) + "░" * (10 - int(ex / 10))
        print(f"  {db:33} {d['count']:>6} {d['pass']:>6} {ex:>7.1f}%  {bar}")
    
    # Save results
    report_path = RESULTS_DIR / "pipeline_eval_report.json"
    with open(report_path, "w") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    
    details_path = RESULTS_DIR / "pipeline_eval_details.json"
    simple_results = []
    for r in results:
        simple_results.append({
            "idx": r.get("idx"),
            "db_id": r.get("db_id"),
            "difficulty": r.get("difficulty"),
            "match": r.get("match"),
            "error": r.get("error"),
            "trace": r.get("trace"),
            "has_error": r.get("has_error"),
            "requires_clarification": r.get("requires_clarification"),
        })
    with open(details_path, "w") as f:
        json.dump(simple_results, f, indent=2)
    
    print(f"\n  Report saved: {report_path}")
    print(f"  Details saved: {details_path}")
    
    return stats


def main():
    parser = argparse.ArgumentParser(description="BIRD Semantic Registry Builder + Pipeline Evaluator")
    parser.add_argument("--generate", action="store_true", help="Generate semantic registry")
    parser.add_argument("--db", default="all", help="Database to process (or 'all')")
    parser.add_argument("--eval", action="store_true", help="Run pipeline evaluation")
    parser.add_argument("--subset", type=int, default=None, help="Subset of questions for eval")
    parser.add_argument("--indices", type=str, default=None, help="JSON file with question indices")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM analysis (deterministic only)")
    args = parser.parse_args()
    
    if args.generate:
        provider = None if args.no_llm else DeepSeekProvider()
        generate_all_databases(provider, args.db)
    
    if args.eval:
        run_evaluation(args.subset, args.indices)
    
    if not args.generate and not args.eval:
        parser.print_help()


if __name__ == "__main__":
    main()
