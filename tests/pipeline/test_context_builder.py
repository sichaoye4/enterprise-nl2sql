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
    assert "Database Schema:" in prompt
    assert "CREATE TABLE orders" in prompt
    assert "paid_gmv_amt" in prompt
    assert "Physical tables may have additional columns beyond those listed above" in prompt
    assert "Do not invent tables" in prompt
    assert "Use SQLite dialect." in prompt
    assert "Generation rules:" in prompt
    assert "Generate exactly one SELECT statement." in prompt
    assert "When a filtered column name appears" in prompt
    assert "<generation_context>" not in prompt
    assert "Output JSON format:" not in prompt
    assert "email" not in prompt.lower()


def test_bird_context_prompt_includes_targeted_generation_rules(registry_data) -> None:
    builder = ContextBuilder(registry_data=registry_data)
    semantic_plan = SemanticQueryPlan(domain="card_games")
    raw_schema = """Database Schema for: card_games

CREATE TABLE cards (
  id TEXT,
  name TEXT,
  borderColor TEXT,
  isFullArt INTEGER
)"""

    prompt = builder.build(
        "Among black card borders, which card has full artwork?",
        semantic_plan,
        RetrievalResult(),
        raw_schema=raw_schema,
        evidence="Use id for card identity.",
    )

    assert "IMPORTANT:" in prompt
    assert "Return ONLY the columns the question explicitly asks for" in prompt
    assert "Never add extra columns like id, date, or height unless explicitly requested" in prompt
    assert "When asked for a 'full name', return the component name columns separately" in prompt
    assert "Name all cards" in prompt
    assert "return the id column from the cards table, NOT the name column" in prompt
    assert "use the exact case shown in sample values" in prompt
    assert "introduction refers to url" in prompt
    assert "Only use COUNT(DISTINCT column)" in prompt
    assert "Do not add IS NOT NULL filters unless the question explicitly mentions null or missing values" in prompt
    assert "WHERE column LIKE '2010-07-19 19:37:33%'" in prompt
    assert "Return ONLY a JSON object:" in prompt
