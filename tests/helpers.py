from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.semantic_registry.models.base import Base


def sqlite_engine() -> AsyncEngine:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def attach_semantic_schema(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS semantic")
        cursor.close()

    return engine


async def create_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def drop_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


async def session_fixture(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    await create_tables(engine)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        try:
            yield session
        finally:
            await session.rollback()
    await drop_tables(engine)


@pytest.fixture
def in_memory_engine() -> AsyncEngine:
    return sqlite_engine()


@pytest.fixture
async def in_memory_session(in_memory_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async for session in session_fixture(in_memory_engine):
        yield session


def write_yaml(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def valid_concept_yaml(name: str = "gmv_concept") -> str:
    return f"""
concept: {name}
display_name: Gross Merchandise Value
domain: finance
definition: Gross merchandise value definition.
type: metric_concept
owner: analytics
status: certified
"""


def valid_entity_yaml(name: str = "order") -> str:
    return f"""
entity: {name}
description: Order entity.
primary_keys:
  - order_id
related_entities: []
status: certified
"""


def valid_dimension_yaml(name: str = "order_date", entity: str = "order") -> str:
    return f"""
dimension: {name}
description: Order date.
entity: {entity}
synonyms: []
physical_mappings:
  - table: orders
    column: order_date
status: certified
"""


def valid_metric_yaml(name: str = "gmv", concept: str = "gmv_concept") -> str:
    return f"""
metric: {name}
concept: {concept}
description: Gross merchandise value.
type: simple_sum
measure:
  table: orders
  column: amount
aggregation: sum
unit: USD
allowed_dimensions:
  - order_date
owner: analytics
status: certified
"""


def valid_term_yaml(name: str = "gmv", concept: str = "gmv_concept", domain: str = "finance") -> str:
    return f"""
term: {name}
description: Gross merchandise value.
synonyms:
  - gross merchandise value
candidate_concepts:
  - {concept}
default_concept_by_domain:
  {domain}: {concept}
ambiguity_level: low
clarification_required_when: []
owner: analytics
domain: {domain}
status: certified
"""


def valid_join_path_yaml(name: str = "orders_to_customers") -> str:
    return f"""
join_path_name: {name}
from_table: orders
to_table: customers
relationship: many_to_one
join_condition: orders.customer_id = customers.customer_id
safe_for_metrics:
  - gmv
fanout_risk: low
notes: Safe for order metrics.
status: certified
"""


def write_valid_semantic_tree(root: Path) -> Path:
    write_yaml(root / "concepts" / "gmv_concept.yaml", valid_concept_yaml())
    write_yaml(root / "entities" / "order.yaml", valid_entity_yaml())
    write_yaml(root / "dimensions" / "order_date.yaml", valid_dimension_yaml())
    write_yaml(root / "metrics" / "gmv.yaml", valid_metric_yaml())
    write_yaml(root / "terms" / "gmv.yaml", valid_term_yaml())
    write_yaml(root / "join_paths" / "orders_to_customers.yaml", valid_join_path_yaml())
    return root
