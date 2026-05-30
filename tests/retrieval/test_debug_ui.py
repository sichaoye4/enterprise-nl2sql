from __future__ import annotations

from fastapi.testclient import TestClient

from src.semantic_registry.api.app import create_app


def test_debug_retrieval_page_returns_200() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/debug/retrieval")

    assert response.status_code == 200
    assert "Retrieval Debug" in response.text


def test_debug_retrieval_search_returns_empty_result_without_configured_retriever() -> None:
    with TestClient(create_app()) as client:
        response = client.post("/debug/retrieval/search", json={"query": "orders", "top_k": 5})

    assert response.status_code == 200
    assert response.json()["candidate_tables"] == []
