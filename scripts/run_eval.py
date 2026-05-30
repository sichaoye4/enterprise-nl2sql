#!/usr/bin/env python3
"""Enterprise NL2SQL — Full Benchmark Evaluation Runner.

Loads all eval cases, runs semantic + SQL eval, prints detailed report.
"""

import sys
import os
sys.path.insert(0, os.path.expanduser("~/enterprise-nl2sql"))

from pathlib import Path
from src.semantic_registry.evaluation.cases import EvalCaseStore
from src.semantic_registry.evaluation.runner import EvalRunner
from src.semantic_registry.pipeline import NL2SQLPipeline
from src.semantic_registry.resolver.registry import load_semantic_registry


def load_all_cases() -> EvalCaseStore:
    store = EvalCaseStore()
    eval_dir = Path("eval_cases")
    for yaml_file in sorted(eval_dir.glob("*.yaml")):
        cases = EvalCaseStore.load_cases_from_yaml(str(eval_dir))
        for case in cases:
            existing = store.get_case(case.case_id)
            if existing is None:
                store.add_case(case)
    return store


def print_separator(char="=", width=80):
    print(char * width)


def print_header(text):
    print_separator()
    print(f"  {text}")
    print_separator()


def print_result_card(case_result, idx, total):
    status = "✅ PASS" if case_result.passed else "❌ FAIL"
    print(f"\n  [{idx+1}/{total}] {case_result.case_id}  {status}")
    print(f"      Question: {case_result.generated_plan.get('metric', '?') if case_result.generated_plan and case_result.generated_plan.get('metric') else case_result.gold_sql[:60] if case_result.gold_sql else '(none)'}")
    if case_result.errors:
        for e in case_result.errors[:3]:
            print(f"      ⚠  {e}")


def print_group_result(name, result, indent=""):
    rate = result.success_rate * 100
    bar_len = int(rate / 5)
    bar = "█" * bar_len + "░" * (20 - bar_len)
    print(f"{indent}{name:30s}  {bar}  {result.passed:3d}/{result.total_cases:2d}  ({rate:.1f}%)")


def main():
    print_header("Enterprise NL2SQL — Full Benchmark Evaluation")
    print()

    # Load registry and pipeline
    print("  Loading semantic registry...", end=" ")
    registry = load_semantic_registry("semantic/")
    print(f"{len(registry.terms)} terms, {len(registry.concepts)} concepts, {len(registry.metrics)} metrics")

    print("  Initializing pipeline...", end=" ")
    pipeline = NL2SQLPipeline(registry_data=registry)
    print("OK")

    # Load eval cases
    store = load_all_cases()
    all_cases = store.list_cases(active_only=True)
    print(f"  Loaded {len(all_cases)} active eval cases\n")

    runner = EvalRunner()

    # ── Semantic Evaluation ──
    print_header("▶  STAGE 1: Semantic Resolution")
    semantic_result = runner.run_semantic_eval(all_cases, pipeline)
    print(f"\n  Overall: {semantic_result.passed}/{semantic_result.total_cases} passed ({semantic_result.success_rate:.1%})")
    for metric, value in sorted(semantic_result.metrics.items()):
        print(f"    {metric:35s} {value:.1%}")

    # ── SQL Evaluation ──
    print()
    print_header("▶  STAGE 2: SQL Generation")
    sql_result = runner.run_sql_eval(all_cases, pipeline)
    print(f"\n  Overall: {sql_result.passed}/{sql_result.total_cases} passed ({sql_result.success_rate:.1%})")
    for metric, value in sorted(sql_result.metrics.items()):
        print(f"    {metric:35s} {value:.1%}")

    # ── Detailed results ──
    print()
    print_header("▶  DETAILED RESULTS")

    # Group by difficulty
    from src.semantic_registry.evaluation.models import EvalCase
    by_difficulty = {"easy": [], "medium": [], "hard": []}
    for case in all_cases:
        by_difficulty.setdefault(case.difficulty, []).append(case)

    print("\n  By Difficulty:")
    for diff in ["easy", "medium", "hard"]:
        group_cases = by_difficulty.get(diff, [])
        if not group_cases:
            continue
        result = runner.run_sql_eval(group_cases, pipeline)
        print_group_result(f"    {diff}", result)

    # Group by tag
    tag_results = {}
    for case in all_cases:
        for tag in case.tags:
            if tag not in tag_results:
                tag_results[tag] = []
            tag_results[tag].append(case)

    print("\n  By Category:")
    for tag in sorted(tag_results.keys()):
        group_cases = tag_results[tag]
        result = runner.run_sql_eval(group_cases, pipeline)
        print_group_result(f"    {tag}", result)

    # ── Per-case results ──
    print()
    print_header("▶  PER-CASE BREAKDOWN")
    for i, case_result in enumerate(sql_result.case_results):
        print_result_card(case_result, i, sql_result.total_cases)

    # ── Summary ──
    print()
    print_separator("─")
    semantic_rate = semantic_result.success_rate * 100
    sql_rate = sql_result.success_rate * 100
    target = 75.0
    semantic_passed = "✅" if semantic_rate >= 85.0 else "⚠️ " if semantic_rate >= 70.0 else "❌"
    sql_passed = "✅" if sql_rate >= target else "❌"

    print(f"""
  ┌──────────────────────────────────────────────┐
  │          EVALUATION SUMMARY                  │
  ├──────────────────────────────────────────────┤
  │  Semantic Resolution:  {semantic_passed}  {semantic_rate:.1f}%  (target: ≥85%)  │
  │  SQL Generation:       {sql_passed}  {sql_rate:.1f}%  (target: ≥{target:.0f}%)  │
  │  Total cases:          {sql_result.total_cases}                             │
  └──────────────────────────────────────────────┘
""")

    # Safety critical checks
    print_header("▶  SAFETY GATE CHECK")
    pass_all = True
    for tag_check, tag, expected, label in [
        ("write_intent blocked", "write_intent", "blocked", "DDL/DML block rate"),
        ("sensitive_data blocked", "pii", "blocked", "PII detection"),
    ]:
        group = tag_results.get(tag, [])
        if group:
            result = runner.run_sql_eval(group, pipeline)
            passed = result.passed == result.total_cases
            status = "✅" if passed else "❌"
            if not passed:
                pass_all = False
            print(f"    {status} {label}: {result.passed}/{result.total_cases}  ({result.success_rate:.0%})")
        else:
            print(f"    ⚪ {label}: no test cases")

    print(f"\n  Overall safety gate: {'✅ ALL PASS' if pass_all else '❌ HAS FAILURES'}")
    print()

    # Pilot readiness
    print_header("▶  PILOT READINESS CHECKLIST")
    checks = [
        (sql_rate >= target, f"SQL eval success rate ≥ {target:.0f}%"),
        (semantic_rate >= 80.0, "Semantic resolution ≥ 80%"),
        (pass_all, "All safety gates pass"),
    ]
    for passed, label in checks:
        print(f"    {'✅' if passed else '❌'} {label}")

    is_ready = all(p for p, _ in checks)
    print(f"\n    {'🎉 READY FOR PILOT' if is_ready else '🔴 NOT YET READY — fix blocking failures above'}")
    print_separator()


if __name__ == "__main__":
    main()
