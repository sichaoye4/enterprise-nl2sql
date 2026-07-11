from __future__ import annotations

from src.semantic_registry.pipeline.candidate_generator import SQLCandidate
from src.semantic_registry.pipeline.state_machine import NL2SQLPipeline, PipelineContext
from src.semantic_registry.resolver.plan import SemanticQueryPlan
from src.semantic_registry.retrieval.hybrid import RetrievalResult
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


class RecordingCandidateGenerator:
    def __init__(self) -> None:
        self.prompt: str | None = None
        self.calls = 0

    def generate_candidates(self, context):
        self.calls += 1
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


class RetryCandidateGenerator:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str | None] = []

    def generate_candidates(self, context):
        self.calls += 1
        self.prompts.append(context.context_prompt)
        if self.calls == 1:
            return [
                SQLCandidate(
                    candidate_id="bad_a",
                    sql="SELECT bogus FROM missing_table",
                    generation_strategy="guarded_llm",
                    assumptions=[],
                    tables_used=["missing_table"],
                    columns_used=["bogus"],
                    confidence="low",
                    reasoning_summary="Invalid guarded candidate.",
                    parse_success=True,
                    validation_errors=[],
                ),
                SQLCandidate(
                    candidate_id="bad_b",
                    sql="SELECT also_bad FROM missing_table",
                    generation_strategy="guarded_llm",
                    assumptions=[],
                    tables_used=["missing_table"],
                    columns_used=["also_bad"],
                    confidence="low",
                    reasoning_summary="Invalid guarded candidate.",
                    parse_success=True,
                    validation_errors=[],
                ),
            ]
        return [
            SQLCandidate(
                candidate_id="raw",
                sql="SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders WHERE payment_dt IS NOT NULL",
                generation_strategy="raw_fallback",
                assumptions=[],
                tables_used=["orders"],
                columns_used=["paid_gmv_amt", "payment_dt"],
                confidence="medium",
                reasoning_summary="Generated without semantic guardrail contract.",
                parse_success=True,
                validation_errors=[],
            )
        ]


def test_semantic_sql_route_uses_compiled_candidate_without_generation(registry_data) -> None:
    compiled_sql = "SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders"
    generator = RecordingCandidateGenerator()
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
        candidate_generator=generator,
    )

    context = pipeline.run("show paid GMV by channel")

    assert context.semantic_route == "SEMANTIC_ASSISTED_LLM"
    assert context.semantic_compiled_sql is None
    assert generator.calls == 1
    assert context.response is not None
    assert context.response.generated_sql.startswith("SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders")
    assert "extract_terms" not in context.trace
    assert "resolve_semantics" not in context.trace
    assert "retrieve_metadata" not in context.trace
    assert "validate" in context.trace
    assert "repair" in context.trace
    assert "select" in context.trace


def test_semantic_assisted_route_passes_contract_and_context_to_llm(registry_data) -> None:
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
        semantic_engine=FakeSemanticEngine(
            {
                "route": "SEMANTIC_ASSISTED_LLM",
                "guardrail_contract": contract,
                "semantic_context": {"resolved_members": ["orders.paid_gmv"]},
            }
        ),
        candidate_generator=generator,
    )

    context = pipeline.run("show paid GMV by channel")

    assert context.semantic_route == "SEMANTIC_ASSISTED_LLM"
    assert context.guardrail_contract == contract
    assert generator.prompt is not None
    assert "<guardrail_contract>" in generator.prompt
    assert '"selected_view": "commerce_orders"' in generator.prompt
    assert '"paid_gmv_amt"' in generator.prompt
    assert "<semantic_context>" in generator.prompt


def test_semantic_quality_gate_catches_orphan_filter_and_downgrades(registry_data) -> None:
    compiled_sql = "SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders WHERE Currency = 'CZK'"
    generator = RecordingCandidateGenerator()
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
        candidate_generator=generator,
    )

    context = pipeline.run("show paid GMV")

    assert context.semantic_route == "SEMANTIC_ASSISTED_LLM"
    assert context.semantic_compiled_sql is None
    assert context.gap_report is not None
    assert context.gap_report["quality_gate"]["orphan_filters"] == ["CZK"]
    assert generator.prompt is not None
    assert "[Compiled SQL Seed]" not in generator.prompt
    assert "<guardrail_contract>" in generator.prompt


def test_semantic_quality_gate_passes_clean_sql(registry_data) -> None:
    compiled_sql = "SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders"
    generator = RecordingCandidateGenerator()
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
        candidate_generator=generator,
    )

    context = pipeline.run("show paid GMV")

    assert context.semantic_route == "SEMANTIC_ASSISTED_LLM"
    assert context.semantic_compiled_sql is None
    assert generator.calls == 1


def test_guarded_llm_retries_without_contract_after_validation_failures(registry_data) -> None:
    generator = RetryCandidateGenerator()
    contract = {
        "selected_view": "commerce_orders",
        "entities": [{"name": "orders", "table": "orders"}],
        "measures": [{"name": "paid_gmv", "column": "paid_gmv_amt", "aggregation": "sum"}],
        "dimensions": [],
        "relationships": [],
    }
    pipeline = NL2SQLPipeline(
        registry_data=registry_data,
        semantic_engine=FakeSemanticEngine({"route": "SEMANTIC_ASSISTED_LLM", "guardrail_contract": contract}),
        candidate_generator=generator,
    )

    context = pipeline.run("show paid GMV")

    assert generator.calls == 2
    assert context.semantic_retry_count == 2
    assert context.semantic_route == "BASELINE_LLM"
    assert context.guardrail_contract is None
    assert context.response is not None
    assert context.response.validation_status == "pass"
    assert context.response.generated_sql == "SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders WHERE payment_dt IS NOT NULL"
    assert generator.prompts[0] is not None and "<guardrail_contract>" in generator.prompts[0]
    assert generator.prompts[1] is not None and "<guardrail_contract>" not in generator.prompts[1]


def test_clarify_route_returns_a_clarification_without_generating_sql(registry_data) -> None:
    generator = RecordingCandidateGenerator()
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
        candidate_generator=generator,
    )

    context = pipeline.run("show paid GMV by channel")

    assert context.semantic_route == "CLARIFY"
    # CLARIFY does NOT short-circuit — pipeline continues to LLM stages
    assert context.requires_clarification is True
    assert context.gap_report is not None
    assert "profit" in str(context.gap_report)
    assert generator.calls == 0
    assert context.response is not None
    assert context.response.generated_sql == ""


def test_build_context_injects_gap_report_for_semantic_assisted_route(registry_data) -> None:
    pipeline = NL2SQLPipeline(
        registry_data=registry_data,
        semantic_engine=FakeSemanticEngine({"route": "SEMANTIC_ASSISTED_LLM"}),
    )
    context = PipelineContext(
        question="show profit by channel",
        domain="commerce",
        semantic_route="SEMANTIC_ASSISTED_LLM",
        gap_report={
            "unresolved_terms": ["profit"],
            "missing_members": ["profit_margin"],
            "suggested_model_additions": ["profit metric"],
        },
        semantic_plan=SemanticQueryPlan(metric="paid_gmv", dimension="channel", domain="commerce"),
        retrieved_metadata=RetrievalResult(),
    )

    context = pipeline.build_context(context)

    assert context.context_prompt is not None
    assert "[Semantic Engine Gap Report]" in context.context_prompt
    assert 'Unresolved terms: ["profit"]' in context.context_prompt
    assert 'Missing members: ["profit_margin"]' in context.context_prompt
    assert 'Suggested model additions: ["profit metric"]' in context.context_prompt


def test_rejected_route_sets_error(registry_data) -> None:
    pipeline = NL2SQLPipeline(
        registry_data=registry_data,
        semantic_engine=FakeSemanticEngine(
            {
                "route": "REJECTED",
                "gap_report": {
                    "unresolved_terms": ["raw_orders"],
                    "missing_members": ["raw_orders"],
                },
            }
        ),
    )

    context = pipeline.run("show paid GMV by channel")

    assert context.semantic_route == "REJECTED"
    assert context.error is not None
    assert "Semantic engine rejected this question." in context.error
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
