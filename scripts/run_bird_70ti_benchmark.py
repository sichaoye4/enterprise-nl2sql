#!/usr/bin/env python3
"""Run a 70-question BIRD benchmark with split router/generation/judge models."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.run_bird_full_eval import (  # noqa: E402
    DEFAULT_BASELINE,
    DEFAULT_DB_ROOT_REQUESTED,
    DEFAULT_DEV_JSON,
    DEFAULT_INDICES,
    DEFAULT_SEMANTIC_MODEL_PATH,
    DEFAULT_SEMANTIC_REGISTRY_ROOT,
    breakdown,
    evaluate_baseline,
    evaluate_pipeline_question,
    load_env_file,
    load_json,
    print_breakdown,
    resolve_db_path,
)
from src.semantic_registry.pipeline import CandidateGenerator, DeepSeekProvider, LLMGateway, LLMJudge, NL2SQLPipeline  # noqa: E402
from src.semantic_registry.pipeline.state_machine import RegistryMetadataProvider  # noqa: E402
from src.semantic_registry.repair.repair_loop import RepairLoop  # noqa: E402
from src.semantic_registry.resolver.registry import load_semantic_registry  # noqa: E402


DEFAULT_OUTPUT = ROOT / "bird_bench" / "results" / "bird_70ti_v4_pro_high_benchmark.json"


class ConfiguredPipelineCache:
    def __init__(
        self,
        *,
        registry_root: Path,
        semantic_model_path: Path,
        router_gateway: LLMGateway,
        generation_gateway: LLMGateway,
        judge: LLMJudge,
    ) -> None:
        self.registry_root = registry_root
        self.semantic_model_path = semantic_model_path
        self.router_gateway = router_gateway
        self.generation_gateway = generation_gateway
        self.judge = judge
        self._pipelines: dict[str, NL2SQLPipeline] = {}

    def get(self, db_id: str) -> NL2SQLPipeline:
        if db_id not in self._pipelines:
            registry_data = load_semantic_registry(self.registry_root / db_id)
            metadata_provider = RegistryMetadataProvider(registry_data)
            self._pipelines[db_id] = NL2SQLPipeline(
                registry_data=registry_data,
                metadata_provider=metadata_provider,
                semantic_model_path=self.semantic_model_path,
                candidate_generator=CandidateGenerator(llm_gateway=self.generation_gateway),
                repair_loop=RepairLoop(metadata_provider=metadata_provider, llm_gateway=self.generation_gateway),
                llm_judge=self.judge,
                router_llm_gateway=self.router_gateway,
            )
        return self._pipelines[db_id]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=70, help="Number of sampled BIRD questions to run.")
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

    router_gateway = LLMGateway(provider=DeepSeekProvider(model="deepseek-v4-flash", reasoning_effort=None))
    generation_gateway = LLMGateway(provider=DeepSeekProvider(model="deepseek-v4-flash", reasoning_effort="xhigh"))
    judge_client = DeepSeekProvider(model="deepseek-v4-flash", reasoning_effort="xhigh")
    judge = LLMJudge(client=judge_client)

    dev = load_json(args.dev_json)
    sample_indices = load_json(args.indices)[: args.limit]
    questions = [(idx, dev[idx]) for idx in sample_indices]
    cache = ConfiguredPipelineCache(
        registry_root=args.semantic_registry_root,
        semantic_model_path=args.semantic_model_path,
        router_gateway=router_gateway,
        generation_gateway=generation_gateway,
        judge=judge,
    )

    print("BIRD 70TI split-model benchmark")
    print(f"  questions: {len(questions)} from {args.indices}")
    print("  router: deepseek-v4-flash")
    print("  generation: deepseek-v4-flash reasoning_effort=xhigh")
    print("  judge: deepseek-v4-flash reasoning_effort=xhigh")
    print(f"  output: {args.output}")

    started = time.time()
    results: list[dict[str, Any]] = []
    failures_by_db: dict[str, int] = defaultdict(int)
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
            failures_by_db[db_id] += 1
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
    report: dict[str, Any] = {
        "config": {
            "name": "BIRD 70TI split-model benchmark",
            "router_model": "deepseek-v4-flash",
            "router_reasoning_effort": None,
            "generation_model": "deepseek-v4-pro",
            "generation_reasoning_effort": "high",
            "judge_model": "deepseek-v4-pro",
            "judge_reasoning_effort": "high",
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
        "script_failures_by_db": dict(sorted(failures_by_db.items())),
        "results": results,
    }

    baseline = evaluate_baseline(baseline_path=args.baseline, questions=questions, db_root=args.db_root)
    if baseline is not None:
        report["baseline"] = baseline

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print("\nSummary")
    print(f"  Split-model pipeline EX: {report['ex']:.2f}% ({passed}/{len(results)})")
    if baseline is not None:
        print(
            "  Baseline LLM-only EX: "
            f"{baseline['ex']:.2f}% recomputed ({baseline['passed']}/{baseline['total']}), "
            f"{baseline.get('recorded_ex')}% recorded in file"
        )
    print_breakdown("Per database", report["per_database"])
    print_breakdown("Per route", report["per_route"])
    print(f"\nWrote report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
