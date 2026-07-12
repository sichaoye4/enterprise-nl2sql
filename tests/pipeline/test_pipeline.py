from __future__ import annotations

from src.semantic_registry.pipeline.state_machine import NL2SQLPipeline
from tests.resolver.conftest import (  # noqa: F401
    registry_data as registry_data,
    resolver_concepts as resolver_concepts,
    resolver_dimensions as resolver_dimensions,
    resolver_metrics as resolver_metrics,
    resolver_terms as resolver_terms,
)


class FakeSemanticEngine:
    def __init__(self, result: dict) -> None:
        self.result = result

    def process(self, question: str):
        return self.result


def test_full_pipeline_run_returns_response_with_sql(registry_data) -> None:
    pipeline = NL2SQLPipeline(registry_data=registry_data)

    context = pipeline.run("show me paid GMV by channel")

    assert context.response is not None
    assert context.response.generated_sql.startswith("SELECT")
    assert context.response.validation_status in ("pass", "fail")


def test_pipeline_stops_on_write_intent_without_sql(registry_data) -> None:
    pipeline = NL2SQLPipeline(registry_data=registry_data)

    context = pipeline.run("insert into orders values (1)")

    assert context.response is not None
    assert context.error is not None
    assert context.response.generated_sql == ""
    assert "Write intent detected" in context.response.error


def test_pipeline_stops_on_sensitive_intent_without_sql(registry_data) -> None:
    pipeline = NL2SQLPipeline(registry_data=registry_data)

    context = pipeline.run("show me user email addresses")

    assert context.response is not None
    assert context.error is not None
    assert context.response.generated_sql == ""
    assert "Sensitive data intent detected" in context.response.error


def test_pipeline_stops_with_clarification_for_ambiguous_revenue(registry_data) -> None:
    pipeline = NL2SQLPipeline(registry_data=registry_data)

    context = pipeline.run("show revenue")

    assert context.response is not None
    assert context.semantic_route == "BASELINE_LLM"
    assert context.error is None


def test_pipeline_context_trace_captures_all_success_steps(registry_data) -> None:
    pipeline = NL2SQLPipeline(registry_data=registry_data)

    context = pipeline.run("show me paid GMV by channel")

    assert context.trace[:4] == [
        "classify",
        "run_semantic_engine",
        "run_semantic_quality_gate",
        "run_semantic_llm_router",
    ]
    assert {"validate", "repair", "select", "explain", "build_response"}.issubset(context.trace)


def test_pipeline_error_handling_builds_error_response(registry_data) -> None:
    class Extractor:
        def extract(self, question: str) -> list:
            return []

    class FailingResolver:
        extractor = Extractor()

        def resolve(self, question: str, domain: str | None = None):
            raise RuntimeError("resolver unavailable")

    pipeline = NL2SQLPipeline(
        registry_data=registry_data,
        resolver=FailingResolver(),
        semantic_engine=FakeSemanticEngine({"route": "BASELINE_LLM"}),
    )

    context = pipeline.run("show paid GMV")

    assert context.response is not None
    assert context.response.generated_sql == ""
    assert context.response.error == "Semantic resolution failed: resolver unavailable"
    assert context.trace == [
        "classify",
        "run_semantic_engine",
        "run_semantic_quality_gate",
        "run_semantic_llm_router",
        "extract_terms",
        "resolve_semantics",
        "build_response",
    ]


def test_post_process_bird_sql_rewrites_formula_1_race_url_to_circuit_url(registry_data) -> None:
    pipeline = NL2SQLPipeline(registry_data=registry_data)
    sql = (
        "SELECT races.url FROM races "
        "INNER JOIN circuits ON races.circuitId = circuits.circuitId "
        "WHERE circuits.name = 'Circuit de Barcelona-Catalunya'"
    )

    processed = pipeline._post_process_bird_sql(
        sql,
        "Where can the introduction of the races held on Circuit de Barcelona-Catalunya be found?",
    )

    assert processed.startswith("SELECT DISTINCT circuits.url FROM races")
    assert "races.url" not in processed


def test_post_process_bird_sql_rewrites_formula_1_race_url_alias(registry_data) -> None:
    pipeline = NL2SQLPipeline(registry_data=registry_data)
    sql = (
        "SELECT r.url FROM races AS r "
        "INNER JOIN circuits AS c ON r.circuitId = c.circuitId "
        "WHERE c.name = 'Sepang International Circuit'"
    )

    processed = pipeline._post_process_bird_sql(
        sql,
        "Where can I find the information about the races held on Sepang International Circuit?",
    )

    assert processed.startswith("SELECT DISTINCT c.url FROM races AS r")
    assert "r.url" not in processed


def test_post_process_bird_sql_leaves_race_url_without_circuit_join(registry_data) -> None:
    pipeline = NL2SQLPipeline(registry_data=registry_data)
    sql = "SELECT races.url FROM races WHERE races.name = 'Australian Grand Prix'"

    processed = pipeline._post_process_bird_sql(sql, "Where can I find the information about this race?")

    assert processed == sql
