from __future__ import annotations

from src.semantic_registry.pipeline.candidate_generator import SQLCandidate
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
        self.questions: list[str] = []

    def process(self, question: str):
        self.questions.append(question)
        return self.result


class FailingCandidateGenerator:
    def generate_candidates(self, context):
        raise AssertionError("LLM candidate generation should have been skipped")


class RecordingCandidateGenerator:
    def __init__(self) -> None:
        self.prompt: str | None = None

    def generate_candidates(self, context):
        self.prompt = context.context_prompt
        return [
            SQLCandidate(
                candidate_id="guarded",
                sql="SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders",
                generation_strategy="guarded_llm",
                assumptions=[],
                tables_used=["orders"],
                columns_used=["paid_gmv_amt"],
                confidence="high",
                reasoning_summary="Generated with semantic guardrails.",
                parse_success=True,
                validation_errors=[],
            )
        ]


def test_semantic_sql_route_bypasses_llm_and_returns_compiled_sql(registry_data) -> None:
    compiled_sql = "SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders"
    pipeline = NL2SQLPipeline(
        registry_data=registry_data,
        semantic_engine=FakeSemanticEngine(
            {
                "route": "SEMANTIC_SQL",
                "compiled_query": {
                    "sql": compiled_sql,
                    "lineage": {
                        "tables": ["orders"],
                        "measures": {"paid_gmv": {"column": "paid_gmv_amt"}},
                    },
                },
            }
        ),
        candidate_generator=FailingCandidateGenerator(),
    )

    context = pipeline.run("show paid GMV by channel")

    assert context.semantic_route == "SEMANTIC_SQL"
    assert context.semantic_compiled_sql == compiled_sql
    assert context.response is not None
    assert context.response.generated_sql == compiled_sql
    assert "generate_candidates" not in context.trace
    assert "validate" not in context.trace
    assert "repair" not in context.trace


def test_guarded_llm_route_passes_contract_to_llm_context(registry_data) -> None:
    generator = RecordingCandidateGenerator()
    contract = {
        "selected_view": "commerce_orders",
        "entities": [{"name": "orders", "table": "orders"}],
        "measures": [{"name": "paid_gmv", "column": "paid_gmv_amt", "aggregation": "sum"}],
        "dimensions": [{"name": "channel", "column": "channel"}],
        "relationships": [],
    }
    pipeline = NL2SQLPipeline(
        registry_data=registry_data,
        semantic_engine=FakeSemanticEngine({"route": "GUARDED_LLM_SQL", "guardrail_contract": contract}),
        candidate_generator=generator,
    )

    context = pipeline.run("show paid GMV by channel")

    assert context.semantic_route == "GUARDED_LLM_SQL"
    assert context.guardrail_contract == contract
    assert generator.prompt is not None
    assert "<guardrail_contract>" in generator.prompt
    assert '"selected_view": "commerce_orders"' in generator.prompt
    assert '"paid_gmv_amt"' in generator.prompt


def test_clarify_route_sets_requires_clarification(registry_data) -> None:
    pipeline = NL2SQLPipeline(
        registry_data=registry_data,
        semantic_engine=FakeSemanticEngine(
            {
                "route": "CLARIFY",
                "gap_report": {
                    "unresolved_terms": ["profit"],
                    "missing_measures": ["profit"],
                },
            }
        ),
    )

    context = pipeline.run("show paid GMV by channel")

    assert context.semantic_route == "CLARIFY"
    assert context.requires_clarification is True
    assert context.clarification is not None
    assert "profit" in context.clarification.message
    assert context.trace == ["classify", "extract_terms", "resolve_semantics", "run_semantic_engine", "build_response"]


def test_blocked_route_sets_error(registry_data) -> None:
    pipeline = NL2SQLPipeline(
        registry_data=registry_data,
        semantic_engine=FakeSemanticEngine(
            {
                "route": "BLOCKED",
                "gap_report": {
                    "unresolved_terms": ["raw_orders"],
                    "missing_members": ["raw_orders"],
                },
            }
        ),
    )

    context = pipeline.run("show paid GMV by channel")

    assert context.semantic_route == "BLOCKED"
    assert context.error is not None
    assert "Semantic engine blocked this question." in context.error
    assert "raw_orders" in context.error
    assert context.response is not None
    assert context.response.generated_sql == ""


def test_semantic_model_path_uses_domain_model_file(registry_data, tmp_path) -> None:
    model_root = tmp_path / "bird_semantic_engine"
    model_file = model_root / "california_schools" / "model.yml"
    model_file.parent.mkdir(parents=True)
    model_file.write_text("version: 1\n", encoding="utf-8")
    fallback = model_root / "commerce" / "model.yml"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("version: 1\n", encoding="utf-8")

    pipeline = NL2SQLPipeline(registry_data=registry_data, semantic_model_path=model_root)

    assert pipeline._semantic_model_path_for_domain("california_schools") == model_file
    assert pipeline._semantic_model_path_for_domain("unknown") == model_root
