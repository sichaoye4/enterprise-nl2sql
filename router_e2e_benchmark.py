"""
Structural e2e benchmark: verify compiled SQL is correct without needing SQLite DB.
Tests: measure filter stripping, new operators (LIKE, BETWEEN), parameter correctness.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "semantic_modeling" / "src"))
sys.path.insert(0, str(Path.home() / "enterprise-nl2sql"))

from semantic_engine.compiler.model_compiler import SemanticModelCompiler
from semantic_engine.loader.yaml_loader import load_semantic_model_file
from src.semantic_registry.pipeline.semantic_router import (
    SemanticRouter,
    compile_from_router,
    RouterResult,
    build_router_prompt,
    _members_by_type,
    _strip_measure_filters,
    SUPPORTED_FILTER_OPERATORS,
)
from semantic_engine.models.query_ir import FilterIR


SEMANTIC_MODEL_DIR = Path.home() / "enterprise-nl2sql" / "bird_semantic_engine"
DB_ID = "debit_card_specializing"


def load_snapshot():
    path = SEMANTIC_MODEL_DIR / DB_ID / "model.yml"
    print(f"  Loading model from: {path}")
    return SemanticModelCompiler().compile(load_semantic_model_file(path))


def check(label, condition, detail=""):
    if condition:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label} — {detail}")
    return condition


def main():
    print("=" * 72)
    print("  Router Fixes — Structural E2E Verification")
    print("=" * 72)

    snapshot = load_snapshot()
    print(f"\n  Snapshot loaded: entities={list(snapshot.entities.keys())}")
    print(f"  Catalog members: {len(snapshot.catalog_index)}")

    identifiers = _members_by_type(snapshot, "identifier")
    print(f"  Identifiers: {identifiers}")

    all_pass = True
    tests_run = 0
    tests_passed = 0

    # ── Verify new operators are in SUPPORTED_FILTER_OPERATORS ────────
    print(f"\n{'─' * 72}")
    print(f"  Pre-check: SUPPORTED_FILTER_OPERATORS")
    print(f"{'─' * 72}")
    
    for op in ["like", "between", "starts_with", "gt", "lt", "contains"]:
        ok = check(f"Operator '{op}' supported", op in SUPPORTED_FILTER_OPERATORS)
        tests_run += 1
        if ok: tests_passed += 1

    # ── Verify _strip_measure_filters ────────────────────────────────
    print(f"\n{'─' * 72}")
    print(f"  Pre-check: _strip_measure_filters")
    print(f"{'─' * 72}")

    count_measure_before = None
    for measure in snapshot.entities["gasstations"].measures:
        if measure.name == "count_gasstationid":
            count_measure_before = measure
            break

    if count_measure_before:
        before_filters = len(count_measure_before.filters)
        print(f"    Before: {before_filters} filters")
        
        cleaned = _strip_measure_filters(snapshot, "gasstations.count_gasstationid")
        
        for measure in cleaned.entities["gasstations"].measures:
            if measure.name == "count_gasstationid":
                after_filters = len(measure.filters)
                print(f"    After: {after_filters} filters")
                ok = check("Filters stripped to 0", after_filters == 0)
                tests_run += 1
                if ok: tests_passed += 1
                
                # Verify original snapshot unchanged
                ok = check("Original snapshot unchanged", 
                    len(count_measure_before.filters) == before_filters)
                tests_run += 1
                if ok: tests_passed += 1

    # ── Test 1: Measure filters stripped with router filters ──────────
    print(f"\n{'─' * 72}")
    print(f"  Test #1: \"How many gas stations in CZE have Premium gasoline?\"")
    print(f"  Goal: No CASE WHEN, proper WHERE clause with router filters")
    print(f"{'─' * 72}")

    router = SemanticRouter(
        snapshot,
        lambda _prompt: json.dumps({
            "measure": "gasstations.count_gasstationid",
            "dimensions": [],
            "time_dimension": None,
            "granularity": None,
            "filters": [
                {"member": "gasstations.country", "operator": "equals", "values": ["CZE"]},
                {"member": "gasstations.segment", "operator": "equals", "values": ["Premium"]},
            ],
            "confidence": 0.91,
        }),
    )
    result = router.route("How many gas stations in CZE have Premium gasoline?")
    
    compiled = compile_from_router(
        snapshot, result, "How many gas stations in CZE have Premium gasoline?"
    )
    
    ok = check("Compiled successfully", compiled is not None)
    tests_run += 1
    if ok:
        tests_passed += 1
        print(f"    SQL: {compiled.sql}")
        print(f"    Parameters: {compiled.parameters}")
        
        ok = check("No CASE WHEN wrapper", "CASE WHEN" not in compiled.sql)
        tests_run += 1
        if ok: tests_passed += 1
        
        ok = check("Has WHERE clause", "WHERE" in compiled.sql)
        tests_run += 1
        if ok: tests_passed += 1
        
        ok = check("Has Country filter", "Country =" in compiled.sql)
        tests_run += 1
        if ok: tests_passed += 1
        
        ok = check("Has Segment filter", "Segment =" in compiled.sql)
        tests_run += 1
        if ok: tests_passed += 1
        
        ok = check("Correct parameters", compiled.parameters == ["CZE", "Premium"])
        tests_run += 1
        if ok: tests_passed += 1
        
        # Parse with sqlglot to verify SQL is valid
        try:
            import sqlglot
            sqlglot.parse_one(compiled.sql.replace("%s", "NULL"))
            ok = check("SQL is valid (sqlglot parse)", True)
        except Exception as e:
            ok = check("SQL is valid (sqlglot parse)", False, str(e))
        tests_run += 1
        if ok: tests_passed += 1
    else:
        tests_run += 5  # skipped checks
        all_pass = False

    # ── Test 2: BETWEEN filter on identifier ─────────────────────────
    print(f"\n{'─' * 72}")
    print(f"  Test #2: \"How much did customer 6 consume ... between Aug and Nov 2013?\"")
    print(f"  Goal: BETWEEN operator, Identifier filter support")
    print(f"{'─' * 72}")

    router2 = SemanticRouter(
        snapshot,
        lambda _prompt: json.dumps({
            "measure": "yearmonth.sum_consumption",
            "dimensions": [],
            "time_dimension": None,
            "granularity": None,
            "filters": [
                {"member": "yearmonth.customerid", "operator": "equals", "values": ["6"]},
                {"member": "yearmonth.date", "operator": "between", "values": ["201308", "201311"]},
            ],
            "confidence": 0.91,
        }),
    )
    result2 = router2.route(
        "How much did customer 6 consume in total between August and November 2013?"
    )
    
    compiled2 = compile_from_router(
        snapshot, result2,
        "How much did customer 6 consume in total between August and November 2013?"
    )
    
    ok = check("Compiled successfully", compiled2 is not None)
    tests_run += 1
    if ok:
        tests_passed += 1
        print(f"    SQL: {compiled2.sql}")
        print(f"    Parameters: {compiled2.parameters}")
        
        ok = check("No CASE WHEN", "CASE WHEN" not in compiled2.sql)
        tests_run += 1
        if ok: tests_passed += 1
        
        ok = check("BETWEEN in SQL", "BETWEEN" in compiled2.sql)
        tests_run += 1
        if ok: tests_passed += 1
        
        # Parameters should be [6-value, between-start, between-end]
        ok = check("Has customer filter param", "6" in compiled2.parameters)
        tests_run += 1
        if ok: tests_passed += 1
        
        ok = check("Has between params", "201308" in compiled2.parameters and "201311" in compiled2.parameters)
        tests_run += 1
        if ok: tests_passed += 1
        
        # Verify parameters match what BETWEEN produces: equals filter first, then BETWEEN values
        expected_params = ["6", "201308", "201311"]
        ok = check(f"Correct parameters ({expected_params})", compiled2.parameters == expected_params)
        tests_run += 1
        if ok: tests_passed += 1
    else:
        tests_run += 5
        all_pass = False

    # ── Test 3: LIKE filter on time dimension ────────────────────────
    print(f"\n{'─' * 72}")
    print(f"  Test #3: \"What was the avg total price of transactions in January 2012?\"")
    print(f"  Goal: LIKE operator, time dimension filter")
    print(f"{'─' * 72}")

    router3 = SemanticRouter(
        snapshot,
        lambda _prompt: json.dumps({
            "measure": "transactions_1k.avg_amount",
            "dimensions": [],
            "time_dimension": None,
            "granularity": None,
            "filters": [
                {"member": "transactions_1k.date", "operator": "like", "values": ["2012-01%"]},
            ],
            "confidence": 0.91,
        }),
    )
    result3 = router3.route(
        "What was the average total price of transactions that occurred in January 2012?"
    )
    
    compiled3 = compile_from_router(
        snapshot, result3,
        "What was the average total price of transactions that occurred in January 2012?"
    )
    
    ok = check("Compiled successfully", compiled3 is not None)
    tests_run += 1
    if ok:
        tests_passed += 1
        print(f"    SQL: {compiled3.sql}")
        print(f"    Parameters: {compiled3.parameters}")
        
        ok = check("LIKE in SQL", "LIKE" in compiled3.sql)
        tests_run += 1
        if ok: tests_passed += 1
        
        ok = check("Correct LIKE pattern", compiled3.parameters == ["2012-01%"])
        tests_run += 1
        if ok: tests_passed += 1
        
        ok = check("No CASE WHEN", "CASE WHEN" not in compiled3.sql)
        tests_run += 1
        if ok: tests_passed += 1
    else:
        tests_run += 3
        all_pass = False

    # ── Test 4: Prompt includes new operators ────────────────────────
    print(f"\n{'─' * 72}")
    print(f"  Test #4: build_router_prompt includes new operators")
    print(f"{'─' * 72}")

    prompt = build_router_prompt(snapshot, "Test question?", db_id=DB_ID)
    
    ok = check("Prompt mentions 'Available identifiers'", "Available identifiers" in prompt)
    tests_run += 1
    if ok: tests_passed += 1
    
    ok = check("Prompt mentions 'Supported filter operators'", "Supported filter operators" in prompt)
    tests_run += 1
    if ok: tests_passed += 1
    
    ok = check("Prompt mentions starts_with", "starts_with" in prompt)
    tests_run += 1
    if ok: tests_passed += 1
    
    ok = check("Prompt has date range rule", "between with inclusive" in prompt)
    tests_run += 1
    if ok: tests_passed += 1

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    print(f"  RESULTS: {tests_passed}/{tests_run} checks passed")
    if tests_passed == tests_run:
        print(f"  ✅ ALL CHECKS PASSED")
    else:
        print(f"  ❌ {tests_run - tests_passed} CHECKS FAILED")
        all_pass = False
    print(f"{'=' * 72}\n")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
