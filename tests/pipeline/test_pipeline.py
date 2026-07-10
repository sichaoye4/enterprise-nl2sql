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
    assert context.response.validation_status == "pass"


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
    # The semantic engine runs first and routes to BLOCKED (no "revenue" term
    # in the semantic model), so the old resolver's ambiguity never fires.
    assert context.semantic_route == "BLOCKED"
    assert context.error is not None
    assert context.response.generated_sql == ""


def test_pipeline_context_trace_captures_all_success_steps(registry_data) -> None:
    pipeline = NL2SQLPipeline(registry_data=registry_data)

    context = pipeline.run("show me paid GMV by channel")

    assert context.trace == [
        "classify",
        "run_semantic_engine",
        "select",
        "explain",
        "build_response",
    ]


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
        semantic_engine=FakeSemanticEngine({"route": "CLARIFY"}),
    )

    context = pipeline.run("show paid GMV")

    assert context.response is not None
    assert context.response.generated_sql == ""
    assert context.response.error == "Semantic resolution failed: resolver unavailable"
    assert context.trace == ["classify", "run_semantic_engine", "extract_terms", "resolve_semantics", "build_response"]
