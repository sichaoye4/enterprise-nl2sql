#!/usr/bin/env python3
"""Run BIRD sample evaluation through the full NL2SQL pipeline.

Pipeline stages exercised:
classify -> extract -> resolve -> semantic_engine -> retrieve -> build_context
-> generate_candidates -> validate -> repair -> select -> explain -> response.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.semantic_registry.pipeline import NL2SQLPipeline  # noqa: E402
from src.semantic_registry.resolver.registry import load_semantic_registry  # noqa: E402


DEFAULT_DEV_JSON = ROOT / "bird_bench" / "dev" / "dev_20240627" / "dev.json"
DEFAULT_INDICES = ROOT / "bird_bench" / "results" / "sample_indices.json"
DEFAULT_DB_ROOT_REQUESTED = ROOT / "bird_bench" / "dev" / "dev_20240627"
DEFAULT_DB_ROOT_CHECKOUT = ROOT / "bird_bench" / "dev" / "dev_20240627" / "databases" / "dev_databases"
DEFAULT_SEMANTIC_REGISTRY_ROOT = ROOT / "bird_semantic"
DEFAULT_SEMANTIC_MODEL_PATH = ROOT / "bird_semantic_engine"
DEFAULT_BASELINE = ROOT / "bird_bench" / "results" / "full_benchmarks" / "full_V4_Flash_few_shot_xhigh.json"
DEFAULT_OUTPUT = ROOT / "bird_bench" / "results" / "bird_full_pipeline_semantic_engine_eval.json"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def resolve_db_path(db_id: str, db_root: Path) -> Path:
    candidates = [
        db_root / db_id / f"{db_id}.sqlite",
        DEFAULT_DB_ROOT_REQUESTED / db_id / f"{db_id}.sqlite",
        DEFAULT_DB_ROOT_CHECKOUT / db_id / f"{db_id}.sqlite",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def execute_sql(db_path: Path, sql: str) -> dict[str, Any]:
    if not sql or not sql.strip():
        return {"ok": False, "rows": [], "row_count": 0, "error": "No SQL produced"}
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        return {"ok": True, "rows": rows, "row_count": len(rows), "error": ""}
    except Exception as exc:
        return {"ok": False, "rows": [], "row_count": 0, "error": str(exc)}
    finally:
        if conn is not None:
            conn.close()


def result_sets_match(predicted: dict[str, Any], gold: dict[str, Any]) -> bool:
    if not predicted["ok"] or not gold["ok"]:
        return False
    return set(predicted["rows"]) == set(gold["rows"])


def safe_rows(rows: list[tuple[Any, ...]], limit: int = 5) -> list[list[Any]]:
    return [list(row) for row in rows[:limit]]


class PipelineCache:
    def __init__(self, registry_root: Path, semantic_model_path: Path) -> None:
        self.registry_root = registry_root
        self.semantic_model_path = semantic_model_path
        self._pipelines: dict[str, NL2SQLPipeline] = {}

    def get(self, db_id: str) -> NL2SQLPipeline:
        if db_id not in self._pipelines:
            semantic_dir = self.registry_root / db_id
            registry_data = load_semantic_registry(semantic_dir)
            self._pipelines[db_id] = NL2SQLPipeline(
                registry_data=registry_data,
                semantic_model_path=self.semantic_model_path,
            )
        return self._pipelines[db_id]


def selected_sql(context: Any) -> str:
    if getattr(context, "response", None) is not None and context.response.generated_sql:
        return context.response.generated_sql
    if getattr(context, "selected_sql", None) is not None and context.selected_sql.sql:
        return context.selected_sql.sql
    for candidate in getattr(context, "sql_candidates", []) or []:
        if candidate.sql:
            return candidate.sql
    return ""


def evaluate_pipeline_question(
    *,
    local_idx: int,
    question_idx: int,
    question: dict[str, Any],
    pipeline: NL2SQLPipeline,
    db_path: Path,
) -> dict[str, Any]:
    started = time.time()
    context = pipeline.run(question["question"], domain=question["db_id"], evidence=question.get("evidence"))
    elapsed = time.time() - started

    sql = selected_sql(context)
    predicted = execute_sql(db_path, sql)
    gold = execute_sql(db_path, question["SQL"])
    match = result_sets_match(predicted, gold)
    semantic_route = getattr(context, "semantic_route", None)
    route = semantic_route or "NOT_RUN"
    route_source = "semantic_engine" if semantic_route else "pre_semantic_pipeline"

    return {
        "idx": local_idx,
        "question_id": question.get("question_id", question_idx),
        "source_idx": question_idx,
        "db_id": question["db_id"],
        "difficulty": question.get("difficulty"),
        "question": question["question"],
        "route": route,
        "route_source": route_source,
        "match": match,
        "sql": sql,
        "gold_sql": question["SQL"],
        "execution": {
            "ok": predicted["ok"],
            "row_count": predicted["row_count"],
            "error": predicted["error"],
        },
        "gold_execution": {
            "ok": gold["ok"],
            "row_count": gold["row_count"],
            "error": gold["error"],
        },
        "sample_predicted_rows": safe_rows(predicted["rows"]),
        "sample_gold_rows": safe_rows(gold["rows"]),
        "requires_clarification": bool(getattr(context, "requires_clarification", False)),
        "error": getattr(context, "error", None),
        "trace": list(getattr(context, "trace", []) or []),
        "guardrail_contract_present": bool(getattr(context, "guardrail_contract", None)),
        "gap_report": getattr(context, "gap_report", None),
        "elapsed_sec": round(elapsed, 3),
    }


def evaluate_baseline(
    *,
    baseline_path: Path,
    questions: list[tuple[int, dict[str, Any]]],
    db_root: Path,
) -> dict[str, Any] | None:
    if not baseline_path.exists():
        return None

    baseline = load_json(baseline_path)
    rows = baseline.get("results", [])
    results: list[dict[str, Any]] = []
    recorded_match_passed = 0
    db_id_mismatches = 0
    for local_idx, (question_idx, question) in enumerate(questions):
        baseline_row = rows[local_idx] if local_idx < len(rows) else {}
        if baseline_row.get("match"):
            recorded_match_passed += 1
        if baseline_row.get("db_id") != question["db_id"]:
            db_id_mismatches += 1
        sql = baseline_row.get("sql") or ""
        db_path = resolve_db_path(question["db_id"], db_root)
        predicted = execute_sql(db_path, sql)
        gold = execute_sql(db_path, question["SQL"])
        match = result_sets_match(predicted, gold)
        results.append(
            {
                "idx": local_idx,
                "question_id": question.get("question_id", question_idx),
                "source_idx": question_idx,
                "db_id": question["db_id"],
                "baseline_recorded_db_id": baseline_row.get("db_id"),
                "baseline_recorded_match": baseline_row.get("match"),
                "match": match,
                "sql": sql,
                "execution": {
                    "ok": predicted["ok"],
                    "row_count": predicted["row_count"],
                    "error": predicted["error"],
                },
            }
        )

    passed = sum(1 for row in results if row["match"])
    return {
        "path": str(baseline_path),
        "recorded_total": baseline.get("total"),
        "recorded_passed": baseline.get("passed"),
        "recorded_ex": baseline.get("ex"),
        "recorded_match_passed_for_rows": recorded_match_passed,
        "recorded_match_ex_for_rows": round(recorded_match_passed / len(results) * 100, 2) if results else 0.0,
        "db_id_mismatches": db_id_mismatches,
        "total": len(results),
        "passed": passed,
        "ex": round(passed / len(results) * 100, 2) if results else 0.0,
        "results": results,
    }


def breakdown(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0, "passed": 0})
    for row in rows:
        value = str(row.get(key) or "UNKNOWN")
        grouped[value]["total"] += 1
        grouped[value]["passed"] += 1 if row.get("match") else 0
    return {
        name: {
            **stats,
            "ex": round(stats["passed"] / stats["total"] * 100, 2) if stats["total"] else 0.0,
        }
        for name, stats in sorted(grouped.items())
    }


def print_breakdown(title: str, data: dict[str, dict[str, Any]]) -> None:
    print(f"\n{title}")
    for name, stats in data.items():
        print(f"  {name:28s} {stats['passed']:3d}/{stats['total']:<3d} {stats['ex']:6.2f}%")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50, help="Number of sample questions to run.")
    parser.add_argument("--indices", type=Path, default=DEFAULT_INDICES, help="Path to sample_indices.json.")
    parser.add_argument("--dev-json", type=Path, default=DEFAULT_DEV_JSON, help="Path to BIRD dev.json.")
    parser.add_argument("--db-root", type=Path, default=DEFAULT_DB_ROOT_REQUESTED, help="BIRD database root.")
    parser.add_argument("--semantic-registry-root", type=Path, default=DEFAULT_SEMANTIC_REGISTRY_ROOT)
    parser.add_argument("--semantic-model-path", type=Path, default=DEFAULT_SEMANTIC_MODEL_PATH)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--env-file", type=Path, default=Path.home() / ".hermes" / ".env")
    parser.add_argument("--no-env-file", action="store_true", help="Do not load ~/.hermes/.env.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    if not args.no_env_file:
        load_env_file(args.env_file)

    dev = load_json(args.dev_json)
    sample_indices = load_json(args.indices)[: args.limit]
    questions = [(idx, dev[idx]) for idx in sample_indices]
    cache = PipelineCache(args.semantic_registry_root, args.semantic_model_path)

    print("BIRD full pipeline evaluation")
    print(f"  questions: {len(questions)} from {args.indices}")
    print(f"  semantic models: {args.semantic_model_path}")
    print(f"  semantic registries: {args.semantic_registry_root}")
    print(f"  output: {args.output}")

    started = time.time()
    results: list[dict[str, Any]] = []
    for local_idx, (question_idx, question) in enumerate(questions):
        db_id = question["db_id"]
        db_path = resolve_db_path(db_id, args.db_root)
        try:
            row = evaluate_pipeline_question(
                local_idx=local_idx,
                question_idx=question_idx,
                question=question,
                pipeline=cache.get(db_id),
                db_path=db_path,
            )
        except Exception as exc:
            row = {
                "idx": local_idx,
                "question_id": question.get("question_id", question_idx),
                "source_idx": question_idx,
                "db_id": db_id,
                "difficulty": question.get("difficulty"),
                "question": question["question"],
                "route": "ERROR",
                "route_source": "script",
                "match": False,
                "sql": "",
                "gold_sql": question["SQL"],
                "execution": {"ok": False, "row_count": 0, "error": str(exc)},
                "requires_clarification": False,
                "error": str(exc),
                "trace": [],
                "guardrail_contract_present": False,
                "gap_report": None,
                "elapsed_sec": 0.0,
            }
        results.append(row)

        passed = sum(1 for item in results if item["match"])
        status = "PASS" if row["match"] else "FAIL"
        print(
            f"  [{local_idx + 1:02d}/{len(questions)}] {status:4s} "
            f"{db_id:28s} route={row['route']:16s} ex={passed / len(results) * 100:6.2f}%"
        )

    passed = sum(1 for row in results if row["match"])
    elapsed_min = (time.time() - started) / 60
    report = {
        "config": {
            "name": "BIRD full pipeline + semantic engine guardrails",
            "semantic_model_path": str(args.semantic_model_path),
            "semantic_registry_root": str(args.semantic_registry_root),
            "indices": str(args.indices),
            "limit": args.limit,
        },
        "total": len(results),
        "passed": passed,
        "ex": round(passed / len(results) * 100, 2) if results else 0.0,
        "time_min": round(elapsed_min, 2),
        "per_database": breakdown(results, "db_id"),
        "per_route": breakdown(results, "route"),
        "results": results,
    }

    baseline = evaluate_baseline(baseline_path=args.baseline, questions=questions, db_root=args.db_root)
    if baseline is not None:
        report["baseline"] = baseline

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print("\nSummary")
    print(f"  Semantic engine full pipeline EX: {report['ex']:.2f}% ({passed}/{len(results)})")
    if baseline is not None:
        print(
            "  Baseline LLM-only EX: "
            f"{baseline['ex']:.2f}% recomputed ({baseline['passed']}/{baseline['total']}), "
            f"{baseline.get('recorded_ex')}% recorded in file"
        )
        if baseline["db_id_mismatches"]:
            print(
                "  Baseline alignment warning: "
                f"{baseline['db_id_mismatches']}/{baseline['total']} rows have a recorded db_id "
                "that differs from the sampled question db_id."
            )
    print_breakdown("Per database", report["per_database"])
    print_breakdown("Per route", report["per_route"])
    print(f"\nWrote report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
