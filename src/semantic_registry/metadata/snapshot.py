from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import Boolean, DateTime, Integer, JSON, String, UniqueConstraint, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import Mapped, mapped_column

from src.semantic_registry.metadata.provider import MetadataProvider
from src.semantic_registry.models import (
    SemanticConcept,
    SemanticDimension,
    SemanticEntity,
    SemanticJoinPath,
    SemanticMetric,
    SemanticPhysicalMapping,
    SemanticTerm,
)
from src.semantic_registry.models.base import Base, GUID


SCHEMA_NAME = "semantic"


class MetadataSnapshot(Base):
    __tablename__ = "metadata_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_version", name="uq_metadata_snapshots_version"),
        {"schema": SCHEMA_NAME},
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    snapshot_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    semantic_registry_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")


def _row_to_dict(row: Any) -> dict[str, Any]:
    mapper = inspect(row).mapper
    return {column.key: getattr(row, column.key) for column in mapper.column_attrs}


async def _dump_model(session: AsyncSession, model: type) -> list[dict[str, Any]]:
    rows = (await session.execute(select(model))).scalars().all()
    return jsonable_encoder([_row_to_dict(row) for row in rows])


async def _dump_semantic_registry(session: AsyncSession) -> dict[str, list[dict[str, Any]]]:
    model_map = {
        "terms": SemanticTerm,
        "concepts": SemanticConcept,
        "metrics": SemanticMetric,
        "dimensions": SemanticDimension,
        "entities": SemanticEntity,
        "join_paths": SemanticJoinPath,
        "physical_mappings": SemanticPhysicalMapping,
    }
    return {name: await _dump_model(session, model) for name, model in model_map.items()}


def _provider_tables(metadata_provider: MetadataProvider) -> list[dict[str, Any]]:
    if hasattr(metadata_provider, "list_tables"):
        tables = metadata_provider.list_tables()
    else:
        tables = metadata_provider.search_tables("")
    return [table.model_dump(mode="json") for table in tables]


def _snapshot_version() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


async def create_snapshot(session: AsyncSession, metadata_provider: MetadataProvider) -> MetadataSnapshot:
    metadata_tables = _provider_tables(metadata_provider)
    semantic_registry = await _dump_semantic_registry(session)
    row_count = len(metadata_tables) + sum(len(rows) for rows in semantic_registry.values())
    await session.execute(update(MetadataSnapshot).where(MetadataSnapshot.is_active.is_(True)).values(is_active=False))
    snapshot = MetadataSnapshot(
        snapshot_version=_snapshot_version(),
        metadata_json={"tables": metadata_tables},
        semantic_registry_json=semantic_registry,
        row_count=row_count,
        is_active=True,
    )
    session.add(snapshot)
    await session.commit()
    await session.refresh(snapshot)
    return snapshot


async def get_active_snapshot(session: AsyncSession) -> MetadataSnapshot | None:
    result = await session.execute(
        select(MetadataSnapshot)
        .where(MetadataSnapshot.is_active.is_(True))
        .order_by(MetadataSnapshot.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_snapshots(session: AsyncSession) -> list[MetadataSnapshot]:
    result = await session.execute(select(MetadataSnapshot).order_by(MetadataSnapshot.created_at.desc()))
    return list(result.scalars().all())


async def restore_snapshot(session: AsyncSession, snapshot_id: uuid.UUID | str) -> dict[str, Any]:
    snapshot = await session.get(MetadataSnapshot, uuid.UUID(str(snapshot_id)))
    if snapshot is None:
        return {}
    return {
        "metadata": snapshot.metadata_json,
        "semantic_registry": snapshot.semantic_registry_json,
        "snapshot_version": snapshot.snapshot_version,
        "created_at": snapshot.created_at,
    }


__all__ = [
    "MetadataSnapshot",
    "create_snapshot",
    "get_active_snapshot",
    "list_snapshots",
    "restore_snapshot",
]
