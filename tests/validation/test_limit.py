from __future__ import annotations

from src.semantic_registry.validation.limit import inject_limit


def test_limit_injected_when_missing() -> None:
    assert inject_limit("SELECT paid_gmv_amt FROM orders", limit=100) == "SELECT paid_gmv_amt FROM orders LIMIT 100"


def test_existing_smaller_limit_preserved() -> None:
    assert inject_limit("SELECT paid_gmv_amt FROM orders LIMIT 10", limit=100) == "SELECT paid_gmv_amt FROM orders LIMIT 10"
