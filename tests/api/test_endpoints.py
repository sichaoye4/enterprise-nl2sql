from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from src.semantic_registry.config import Settings
from src.semantic_registry.models import SemanticConcept, SemanticStatus, SemanticTerm
from tests.helpers import write_valid_semantic_tree


async def seed_terms(session, count: int = 1) -> list[SemanticTerm]:
    terms = [
        SemanticTerm(
            term=f"term_{index}",
            description=f"Term {index}",
            owner="analytics",
            domain="finance" if index % 2 == 0 else "sales",
            status=SemanticStatus.certified if index % 2 == 0 else SemanticStatus.draft,
        )
        for index in range(count)
    ]
    session.add_all(terms)
    await session.commit()
    return terms


def test_list_terms_returns_data_and_pagination(test_client) -> None:
    response = test_client.get("/api/v1/terms")

    assert response.status_code == 200
    assert set(response.json()) == {"data", "pagination"}


@pytest.mark.asyncio
async def test_get_term_by_id_returns_record(test_client, in_memory_session) -> None:
    term = (await seed_terms(in_memory_session))[0]

    response = test_client.get(f"/api/v1/terms/{term.id}")

    assert response.status_code == 200
    assert response.json()["term"] == term.term


def test_get_term_by_id_returns_404_for_missing_uuid(test_client) -> None:
    response = test_client.get(f"/api/v1/terms/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_terms_can_filter_by_domain(test_client, in_memory_session) -> None:
    await seed_terms(in_memory_session, count=3)

    response = test_client.get("/api/v1/terms?domain=finance")

    assert response.status_code == 200
    assert {row["domain"] for row in response.json()["data"]} == {"finance"}


@pytest.mark.asyncio
async def test_terms_can_filter_by_status(test_client, in_memory_session) -> None:
    await seed_terms(in_memory_session, count=4)

    response = test_client.get("/api/v1/terms?status=certified")

    assert response.status_code == 200
    assert {row["status"] for row in response.json()["data"]} == {"certified"}


def test_sync_endpoint_returns_sync_report(test_client, tmp_path: Path) -> None:
    semantic_dir = write_valid_semantic_tree(tmp_path / "semantic")
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:", semantic_dir=semantic_dir)

    with patch("src.semantic_registry.api.app.get_settings", return_value=settings):
        response = test_client.post("/api/v1/sync", json={"dry_run": False})

    assert response.status_code == 200
    assert {"total", "created", "updated", "deprecated", "errors", "skipped"} <= set(response.json())
    assert response.json()["created"] > 0


@pytest.mark.asyncio
async def test_sync_endpoint_dry_run_does_not_persist(test_client, in_memory_session, tmp_path: Path) -> None:
    semantic_dir = write_valid_semantic_tree(tmp_path / "semantic")
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:", semantic_dir=semantic_dir)

    with patch("src.semantic_registry.api.app.get_settings", return_value=settings):
        response = test_client.post("/api/v1/sync", json={"dry_run": True})

    rows = (await in_memory_session.execute(select(SemanticTerm))).scalars().all()
    assert response.status_code == 200
    assert response.json()["created"] > 0
    assert rows == []


@pytest.mark.asyncio
async def test_status_returns_counts_by_entity_type(test_client, in_memory_session) -> None:
    await seed_terms(in_memory_session)
    in_memory_session.add(
        SemanticConcept(
            concept="gmv_concept",
            display_name="GMV",
            domain="finance",
            definition="Definition.",
            owner="analytics",
        )
    )
    await in_memory_session.commit()

    response = test_client.get("/api/v1/status")

    assert response.status_code == 200
    assert response.json()["counts"]["terms"] == 1
    assert response.json()["counts"]["concepts"] == 1


@pytest.mark.asyncio
async def test_terms_pagination_limits_page_size(test_client, in_memory_session) -> None:
    await seed_terms(in_memory_session, count=8)

    response = test_client.get("/api/v1/terms?page=1&page_size=5")

    assert response.status_code == 200
    assert len(response.json()["data"]) <= 5
    assert response.json()["pagination"]["page_size"] == 5


def test_error_response_format(test_client) -> None:
    response = test_client.get(f"/api/v1/terms/{uuid.uuid4()}")

    assert response.status_code == 404
    assert set(response.json()["error"]) == {"code", "message", "details"}


def test_extract_endpoint_returns_extracted_terms(test_client) -> None:
    response = test_client.post("/api/v1/extract", json={"question": "show paid GMV by channel"})

    assert response.status_code == 200
    assert response.json()[0]["term"] == "paid_gmv"


def test_resolve_endpoint_returns_semantic_plan(test_client) -> None:
    response = test_client.post(
        "/api/v1/resolve",
        json={"question": "show paid GMV by channel for last 30 days"},
    )

    assert response.status_code == 200
    assert response.json()["metric"] == "paid_gmv"
    assert response.json()["dimension"] == "channel"


def test_clarify_endpoint_returns_clarification_response(test_client) -> None:
    response = test_client.post("/api/v1/clarify", json={"question": "show revenue", "context": {}})

    assert response.status_code == 200
    assert response.json()["needs_clarification"]
    assert response.json()["options"]


def test_query_history_endpoint_returns_200(test_client) -> None:
    response = test_client.get("/api/v1/queries")

    assert response.status_code == 200
    assert set(response.json()) == {"data", "pagination"}


@pytest.mark.asyncio
async def test_query_feedback_endpoint_returns_200(test_client, in_memory_session) -> None:
    from src.semantic_registry.models import QueryLog

    query_log = QueryLog(
        query_id="query-feedback-1",
        question="show paid GMV",
        generated_sql="SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders",
        semantic_plan_json={},
        validation_results_json={},
        status="success",
        user="analyst",
    )
    in_memory_session.add(query_log)
    await in_memory_session.commit()

    response = test_client.post(
        "/api/v1/queries/query-feedback-1/feedback",
        json={
            "feedback_type": "correct",
            "corrected_sql": "SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders",
            "user_comment": "looks good",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "feedback_recorded"


@pytest.mark.parametrize("path", ["/concepts", "/metrics", "/dimensions", "/entities", "/join-paths"])
def test_other_entity_list_endpoints_return_200(test_client, path: str) -> None:
    response = test_client.get(f"/api/v1{path}")

    assert response.status_code == 200
    assert set(response.json()) == {"data", "pagination"}
