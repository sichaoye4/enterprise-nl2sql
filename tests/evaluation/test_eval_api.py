from __future__ import annotations

from tests.api.conftest import (  # noqa: F401
    in_memory_engine as in_memory_engine,
    in_memory_session as in_memory_session,
    test_client as test_client,
)


def test_get_eval_cases_returns_200(test_client) -> None:
    response = test_client.get("/api/v1/eval/cases")

    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_post_eval_run_returns_200(test_client) -> None:
    response = test_client.post("/api/v1/eval/run", json={})

    assert response.status_code == 200
    assert {"total_cases", "passed", "failed", "success_rate", "case_results", "metrics"} <= set(response.json())
