from __future__ import annotations

from src.semantic_registry.resolver import SemanticResolver


def test_full_resolution_with_clear_input(registry_data) -> None:
    resolver = SemanticResolver(registry_data)

    plan = resolver.resolve("show me paid GMV by channel for last 30 days")

    assert plan.metric == "paid_gmv"
    assert plan.dimension == "channel"
    assert plan.domain == "commerce"
    assert not plan.requires_clarification


def test_pipeline_stops_at_clarification_when_ambiguity_found(registry_data) -> None:
    resolver = SemanticResolver(registry_data)

    plan = resolver.resolve("show revenue")

    assert plan.requires_clarification
    assert "ask_clarification" in resolver.last_trace


def test_exact_match_success_skips_synonym_step(registry_data) -> None:
    resolver = SemanticResolver(registry_data)

    resolver.resolve("show paid GMV")

    assert "exact_match" in resolver.last_trace
    assert "synonym_match" not in resolver.last_trace
