from __future__ import annotations

from src.semantic_registry.pipeline.context_builder import ContextBuilder
from src.semantic_registry.pipeline.state_machine import RegistryMetadataProvider
from src.semantic_registry.resolver.plan import SemanticQueryPlan
from src.semantic_registry.retrieval.hybrid import RetrievalResult, ScoredCandidate
from tests.resolver.conftest import (  # noqa: F401
    registry_data as registry_data,
    resolver_concepts as resolver_concepts,
    resolver_dimensions as resolver_dimensions,
    resolver_metrics as resolver_metrics,
    resolver_terms as resolver_terms,
)


def test_context_prompt_includes_question_schema_semantics_and_rules(registry_data) -> None:
    provider = RegistryMetadataProvider(registry_data)
    builder = ContextBuilder(registry_data=registry_data, metadata_provider=provider)
    semantic_plan = SemanticQueryPlan(
        metric="paid_gmv",
        dimension="channel",
        time_range="last_month",
        time_semantics="payment_date",
        domain="commerce",
    )
    retrieved_metadata = RetrievalResult(
        candidate_tables=[
            ScoredCandidate(
                name="orders",
                score=0.95,
                description="Certified semantic metric source.",
                domain="commerce",
            )
        ],
        candidate_metrics=[
            ScoredCandidate(name="paid_gmv", score=0.9, description="Paid GMV.", domain="commerce")
        ],
    )

    prompt = builder.build("show paid GMV by channel last month", semantic_plan, retrieved_metadata)

    assert "show paid GMV by channel last month" in prompt
    assert "Paid GMV" in prompt
    assert "DDL Schema:" in prompt
    assert "CREATE TABLE `orders`" in prompt
    assert "`paid_gmv_amt` REAL" in prompt
    assert "Use SQLite dialect." in prompt
    assert "Generation rules:" in prompt
    assert "Generate exactly one SELECT statement." in prompt
    assert "When a filtered column name appears" in prompt
    assert "<generation_context>" not in prompt
    assert "Output JSON format:" not in prompt
    assert "email" not in prompt.lower()
