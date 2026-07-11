from __future__ import annotations

import json
from pathlib import Path

import sqlglot
import pytest

from src.semantic_registry.pipeline.semantic_router import (
    SemanticRouter,
    build_router_prompt,
    compile_from_router,
)
from src.semantic_registry.pipeline.state_machine import NL2SQLPipeline
from tests.resolver.conftest import (  # noqa: F401
    registry_data as registry_data,
    resolver_concepts as resolver_concepts,
    resolver_dimensions as resolver_dimensions,
    resolver_metrics as resolver_metrics,
    resolver_terms as resolver_terms,
)

from semantic_engine.compiler.model_compiler import SemanticModelCompiler
from semantic_engine.loader.yaml_loader import load_semantic_model_file
from semantic_engine.models.query_ir import FilterIR


class FakeSemanticEngine:
    def __init__(self, snapshot, result: dict) -> None:
        self.snapshot = snapshot
        self.result = result

    def process(self, question: str):
        return self.result


class RecordingCandidateGenerator:
    calls = 0

    def generate_candidates(self, context):
        self.calls += 1
        return []


@pytest.fixture
def debit_card_snapshot():
    path = Path("bird_semantic_engine/debit_card_specializing/model.yml")
    return SemanticModelCompiler().compile(load_semantic_model_file(path))


def router_json(**overrides) -> str:
    payload = {
        "measure": "gasstations.count_gasstationid",
        "dimensions": [],
        "time_dimension": None,
        "granularity": None,
        "filters": [{"member": "gasstations.country", "operator": "equals", "values": ["CZE"]}],
        "confidence": 0.91,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_router_selects_correct_measure(debit_card_snapshot) -> None:
    router = SemanticRouter(debit_card_snapshot, lambda _prompt: router_json())

    result = router.route("How many gas stations in CZE?", db_id="debit_card_specializing")

    assert result is not None
    assert result.measure == "gasstations.count_gasstationid"


def test_router_detects_no_match(debit_card_snapshot) -> None:
    router = SemanticRouter(
        debit_card_snapshot,
        lambda _prompt: router_json(measure="gasstations.count_gasstationid", confidence=0.1),
    )

    assert router.route("What is the average moon temperature?") is None


def test_router_includes_filters(debit_card_snapshot) -> None:
    router = SemanticRouter(debit_card_snapshot, lambda _prompt: router_json())

    result = router.route("How many gas stations in CZE?")

    assert result is not None
    assert result.filters == [
        FilterIR(member="gasstations.country", operator="equals", values=["CZE"])
    ]


def test_router_compile_success(debit_card_snapshot) -> None:
    router = SemanticRouter(debit_card_snapshot, lambda _prompt: router_json())
    result = router.route("How many gas stations in CZE?")

    compiled = compile_from_router(debit_card_snapshot, result, "How many gas stations in CZE?")

    assert compiled is not None
    assert "FROM gasstations" in compiled.sql
    assert "COUNT(" in compiled.sql
    assert compiled.parameters == ["CZE", "CZE"]
    sqlglot.parse_one(compiled.sql.replace("%s", "NULL"))


def test_router_quality_gate(debit_card_snapshot, registry_data) -> None:
    router = SemanticRouter(
        debit_card_snapshot,
        lambda _prompt: router_json(filters=[], confidence=0.91),
    )
    result = router.route("How many gas stations?")
    compiled = compile_from_router(debit_card_snapshot, result, "How many gas stations?")
    pipeline = NL2SQLPipeline(registry_data=registry_data)

    assert compiled is not None
    assert pipeline._semantic_quality_gate(compiled.sql, "How many gas stations?", compiled.parameters) == ["CZE"]


def test_pipeline_integration(debit_card_snapshot, registry_data) -> None:
    candidate_generator = RecordingCandidateGenerator()
    pipeline = NL2SQLPipeline(
        registry_data=registry_data,
        semantic_engine=FakeSemanticEngine(
            debit_card_snapshot,
            {"route": "CLARIFY", "gap_report": {"unresolved_terms": ["gas stations"]}},
        ),
        candidate_generator=candidate_generator,
    )
    pipeline._llm_router_generate = lambda _prompt: router_json()

    context = pipeline.run("How many gas stations in CZE?", domain="debit_card_specializing")

    assert context.semantic_route == "SEMANTIC_SQL"
    assert context.response is not None
    assert "FROM gasstations" in context.response.generated_sql
    assert context.selected_sql is not None
    assert context.selected_sql.generation_strategy == "semantic_engine"
    assert "run_semantic_llm_router" in context.trace
    assert "generate_candidates" not in context.trace


def test_router_prompt_lists_catalog(debit_card_snapshot) -> None:
    prompt = build_router_prompt(debit_card_snapshot, "How many gas stations in CZE?", "debit_card_specializing")

    assert "gasstations.count_gasstationid" in prompt
    assert "gasstations.country" in prompt
    assert "Respond with ONLY valid JSON" in prompt
