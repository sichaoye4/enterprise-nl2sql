from __future__ import annotations

from src.semantic_registry.evaluation.pilot import PilotManager
from src.semantic_registry.pipeline import NL2SQLPipeline
from tests.resolver.conftest import (  # noqa: F401
    registry_data as registry_data,
    resolver_concepts as resolver_concepts,
    resolver_dimensions as resolver_dimensions,
    resolver_metrics as resolver_metrics,
    resolver_terms as resolver_terms,
)


def test_pilot_whitelist_allows_configured_users() -> None:
    pilot = PilotManager("alice,bob:commerce|finance")

    assert pilot.is_pilot_user("alice") is True
    assert pilot.allowed_domains("alice") == ["*"]
    assert pilot.allowed_domains("bob") == ["commerce", "finance"]


def test_non_whitelisted_user_is_blocked(registry_data, monkeypatch) -> None:
    monkeypatch.setenv("PILOT_USERS", "alice")
    pipeline = NL2SQLPipeline(registry_data=registry_data)

    context = pipeline.run("show paid GMV", domain="commerce", user="mallory")

    assert context.response is not None
    assert context.response.generated_sql == ""
    assert context.response.error == "User is not enabled for the NL2SQL pilot."
