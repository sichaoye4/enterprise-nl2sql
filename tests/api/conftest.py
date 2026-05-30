from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.semantic_registry.api.app import create_app
from src.semantic_registry.database import get_db_session
from tests.helpers import in_memory_engine, in_memory_session

__all__ = ["in_memory_engine", "in_memory_session", "test_client"]


@pytest.fixture
def test_client(in_memory_session: AsyncSession) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield in_memory_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
