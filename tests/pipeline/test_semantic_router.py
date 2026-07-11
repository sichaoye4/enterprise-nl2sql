from __future__ import annotations

import json
from pathlib import Path

import sqlglot
import pytest

from src.semantic_registry.pipeline.semantic_router import (
    SUPPORTED_FILTER_OPERATORS,
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


def test_supported_filter_operators() -> None:
    assert SUPPORTED_FILTER_OPERATORS == {
        "equals",
        "not_equals",
        "not equals",
        "like",
        "not_like",
        "not like",
        "contains",
        "starts_with",
        "ends_with",
        "gt",
        "greater_than",
        "gte",
        ">=",
        "lt",
        "less_than",
        "lte",
        "<=",
        "between",
    }


def test_router_compile_success(debit_card_snapshot) -> None:
    router = SemanticRouter(debit_card_snapshot, lambda _prompt: router_json())
    result = router.route("How many gas stations in CZE?")

    compiled = compile_from_router(debit_card_snapshot, result, "How many gas stations in CZE?")

    assert compiled is not None
    assert "FROM gasstations" in compiled.sql
    assert "COUNT(" in compiled.sql
    assert "CASE WHEN" not in compiled.sql
    assert compiled.parameters == ["CZE"]
    sqlglot.parse_one(compiled.sql.replace("%s", "NULL"))


def test_router_strips_measure_filters_when_router_filters_are_provided(debit_card_snapshot) -> None:
    router = SemanticRouter(
        debit_card_snapshot,
        lambda _prompt: router_json(
            filters=[
                {"member": "gasstations.country", "operator": "equals", "values": ["CZE"]},
                {"member": "gasstations.segment", "operator": "equals", "values": ["Premium"]},
            ]
        ),
    )
    result = router.route("How many gas stations in CZE have Premium gasoline?")

    compiled = compile_from_router(
        debit_card_snapshot,
        result,
        "How many gas stations in CZE have Premium gasoline?",
    )

    assert compiled is not None
    assert "CASE WHEN" not in compiled.sql
    assert "WHERE t0.Country = %s AND t0.Segment = %s" in compiled.sql
    assert compiled.parameters == ["CZE", "Premium"]


def test_router_compiles_between_filter_on_identifier(debit_card_snapshot) -> None:
    router = SemanticRouter(
        debit_card_snapshot,
        lambda _prompt: router_json(
            measure="yearmonth.sum_consumption",
            filters=[
                {"member": "yearmonth.customerid", "operator": "equals", "values": ["6"]},
                {"member": "yearmonth.date", "operator": "between", "values": ["201308", "201311"]},
            ],
        ),
    )
    result = router.route("How much did customer 6 consume in total between August and November 2013?")

    compiled = compile_from_router(
        debit_card_snapshot,
        result,
        "How much did customer 6 consume in total between August and November 2013?",
    )

    assert compiled is not None
    assert "t0.Date BETWEEN %s AND %s" in compiled.sql
    assert compiled.parameters == ["6", "201308", "201311"]


def test_router_compiles_like_filter_on_time_dimension(debit_card_snapshot) -> None:
    router = SemanticRouter(
        debit_card_snapshot,
        lambda _prompt: router_json(
            measure="transactions_1k.avg_amount",
            filters=[
                {"member": "transactions_1k.date", "operator": "like", "values": ["2012-01%"]},
            ],
        ),
    )
    result = router.route("What was the average total price of transactions that occurred in January 2012?")

    compiled = compile_from_router(
        debit_card_snapshot,
        result,
        "What was the average total price of transactions that occurred in January 2012?",
    )

    assert compiled is not None
    assert "t0.Date LIKE %s" in compiled.sql
    assert compiled.parameters == ["2012-01%"]


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
            {"route": "BASELINE_LLM", "gap_report": {"unresolved_terms": ["gas stations"]}},
        ),
        candidate_generator=candidate_generator,
    )
    pipeline._llm_router_generate = lambda _prompt: router_json()

    context = pipeline.run("How many gas stations in CZE?", domain="debit_card_specializing")

    # Router compilation still passes through shared validation. The fixture's
    # commerce registry cannot validate the BIRD tables, so it falls back to
    # semantic-assisted generation rather than returning unchecked SQL.
    assert context.semantic_route == "SEMANTIC_ASSISTED_LLM"
    assert context.response is not None
    assert candidate_generator.calls == 1
    assert "run_semantic_llm_router" in context.trace
    assert "generate_candidates" in context.trace
    assert "validate" in context.trace
    assert context.llm_trace["semantic_router"]["prompt"] is not None
    assert "How many gas stations in CZE?" in context.llm_trace["semantic_router"]["prompt"]
    assert context.llm_trace["semantic_router"]["response"] == router_json()


def test_router_prompt_lists_catalog(debit_card_snapshot) -> None:
    prompt = build_router_prompt(debit_card_snapshot, "How many gas stations in CZE?", "debit_card_specializing")

    assert "gasstations.count_gasstationid" in prompt
    assert "gasstations.country" in prompt
    assert "Available identifiers for filters" in prompt
    assert "between with inclusive bounds" in prompt
    assert "starts_with" in prompt
    assert "Respond with ONLY valid JSON" in prompt
