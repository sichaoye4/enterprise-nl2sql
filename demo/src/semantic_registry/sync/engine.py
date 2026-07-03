from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.semantic_registry.config import get_settings
from src.semantic_registry.database import get_sessionmaker
from src.semantic_registry.models import (
    SemanticConcept,
    SemanticDimension,
    SemanticEntity,
    SemanticJoinPath,
    SemanticMetric,
    SemanticPhysicalMapping,
    SemanticStatus,
    SemanticTerm,
)
from src.semantic_registry.yaml_schema import (
    ConceptYaml,
    DimensionYaml,
    EntityYaml,
    JoinPathYaml,
    MetricYaml,
    PhysicalMappingYaml,
    TermYaml,
    validate_yaml_file,
)
from src.semantic_registry.yaml_schema.schemas import MODEL_BY_DIR, parse_yaml_file


class SyncReport(BaseModel):
    total: int = 0
    created: int = 0
    updated: int = 0
    deprecated: int = 0
    errors: list[str] = Field(default_factory=list)
    skipped: int = 0

    def merge(self, other: SyncReport) -> SyncReport:
        self.total += other.total
        self.created += other.created
        self.updated += other.updated
        self.deprecated += other.deprecated
        self.errors.extend(other.errors)
        self.skipped += other.skipped
        return self


class SyncSpec(BaseModel):
    directory: str
    model: type
    orm_model: type
    key: str
    deprecated_key: str

    model_config = {"arbitrary_types_allowed": True}


SYNC_SPECS: dict[str, SyncSpec] = {
    "terms": SyncSpec(directory="terms", model=TermYaml, orm_model=SemanticTerm, key="term", deprecated_key="term"),
    "concepts": SyncSpec(directory="concepts", model=ConceptYaml, orm_model=SemanticConcept, key="concept", deprecated_key="concept"),
    "metrics": SyncSpec(directory="metrics", model=MetricYaml, orm_model=SemanticMetric, key="metric", deprecated_key="metric"),
    "dimensions": SyncSpec(directory="dimensions", model=DimensionYaml, orm_model=SemanticDimension, key="dimension", deprecated_key="dimension"),
    "entities": SyncSpec(directory="entities", model=EntityYaml, orm_model=SemanticEntity, key="entity", deprecated_key="entity"),
    "join_paths": SyncSpec(directory="join_paths", model=JoinPathYaml, orm_model=SemanticJoinPath, key="join_path_name", deprecated_key="join_path_name"),
    "physical_mappings": SyncSpec(directory="physical_mappings", model=PhysicalMappingYaml, orm_model=SemanticPhysicalMapping, key="semantic_name", deprecated_key="semantic_name"),
}


def _yaml_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")])


def detect_changes(directory: str | Path = "semantic/") -> list[Path]:
    root = Path(directory)
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--", str(root)],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "--", str(root)],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
        )
        paths = [Path(line) for line in [*diff.stdout.splitlines(), *untracked.stdout.splitlines()] if line.endswith((".yaml", ".yml"))]
        return sorted(set(paths))
    except OSError:
        return _yaml_files(root)


def _payload(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=False)


def _row_diff(row: Any, payload: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for key, value in payload.items():
        current = getattr(row, key)
        current_value = current.value if hasattr(current, "value") else current
        if current_value != value:
            changes[key] = value
    return changes


async def _select_existing(session: AsyncSession, orm_model: type, key: str, value: Any) -> Any | None:
    result = await session.execute(select(orm_model).where(getattr(orm_model, key) == value))
    return result.scalar_one_or_none()


async def _sync_spec(
    spec: SyncSpec,
    session: AsyncSession,
    semantic_dir: str | Path | None = None,
    dry_run: bool = False,
    changed_files: set[Path] | None = None,
) -> SyncReport:
    root = Path(semantic_dir or get_settings().semantic_dir)
    directory = root / spec.directory
    report = SyncReport()
    seen_keys: set[str] = set()

    for path in _yaml_files(directory):
        normalized = path
        relative = path if not path.is_absolute() else Path(*path.parts[-3:])
        if changed_files is not None and normalized not in changed_files and relative not in changed_files:
            report.skipped += 1
            continue
        report.total += 1
        validation_errors = validate_yaml_file(path)
        if validation_errors:
            report.errors.extend(str(error) for error in validation_errors)
            continue
        model = parse_yaml_file(path)
        payload = _payload(model)
        key_value = payload[spec.key]
        seen_keys.add(key_value)
        existing = await _select_existing(session, spec.orm_model, spec.key, key_value)
        if existing is None:
            report.created += 1
            if not dry_run:
                session.add(spec.orm_model(**payload))
            continue
        changes = _row_diff(existing, payload)
        if changes:
            report.updated += 1
            if not dry_run:
                for key, value in changes.items():
                    setattr(existing, key, value)
        else:
            report.skipped += 1

    if changed_files is None:
        result = await session.execute(select(spec.orm_model))
        for row in result.scalars():
            key_value = getattr(row, spec.deprecated_key)
            status_value = row.status.value if hasattr(row.status, "value") else row.status
            if key_value not in seen_keys and status_value != SemanticStatus.deprecated.value:
                report.deprecated += 1
                if not dry_run:
                    row.status = SemanticStatus.deprecated

    if not dry_run:
        await session.commit()
    else:
        await session.rollback()
    return report


async def sync_terms(session: AsyncSession, semantic_dir: str | Path | None = None, dry_run: bool = False) -> SyncReport:
    return await _sync_spec(SYNC_SPECS["terms"], session, semantic_dir, dry_run)


async def sync_concepts(session: AsyncSession, semantic_dir: str | Path | None = None, dry_run: bool = False) -> SyncReport:
    return await _sync_spec(SYNC_SPECS["concepts"], session, semantic_dir, dry_run)


async def sync_metrics(session: AsyncSession, semantic_dir: str | Path | None = None, dry_run: bool = False) -> SyncReport:
    return await _sync_spec(SYNC_SPECS["metrics"], session, semantic_dir, dry_run)


async def sync_dimensions(session: AsyncSession, semantic_dir: str | Path | None = None, dry_run: bool = False) -> SyncReport:
    return await _sync_spec(SYNC_SPECS["dimensions"], session, semantic_dir, dry_run)


async def sync_entities(session: AsyncSession, semantic_dir: str | Path | None = None, dry_run: bool = False) -> SyncReport:
    return await _sync_spec(SYNC_SPECS["entities"], session, semantic_dir, dry_run)


async def sync_join_paths(session: AsyncSession, semantic_dir: str | Path | None = None, dry_run: bool = False) -> SyncReport:
    return await _sync_spec(SYNC_SPECS["join_paths"], session, semantic_dir, dry_run)


async def sync_physical_mappings(session: AsyncSession, semantic_dir: str | Path | None = None, dry_run: bool = False) -> SyncReport:
    return await _sync_spec(SYNC_SPECS["physical_mappings"], session, semantic_dir, dry_run)


async def sync_all(
    session: AsyncSession | None = None,
    semantic_dir: str | Path | None = None,
    dry_run: bool = False,
    changed_only: bool = False,
) -> SyncReport:
    report = SyncReport()
    owns_session = session is None
    if session is None:
        session = get_sessionmaker()()
    changed_files = set(detect_changes(semantic_dir or get_settings().semantic_dir)) if changed_only else None
    try:
        for name in ("concepts", "entities", "metrics", "dimensions", "terms", "join_paths", "physical_mappings"):
            report.merge(await _sync_spec(SYNC_SPECS[name], session, semantic_dir, dry_run, changed_files))
        return report
    finally:
        if owns_session:
            await session.close()


def sync_all_sync(semantic_dir: str | Path | None = None, dry_run: bool = False) -> SyncReport:
    async def _run() -> SyncReport:
        async with get_sessionmaker()() as session:
            return await sync_all(session=session, semantic_dir=semantic_dir, dry_run=dry_run)

    return asyncio.run(_run())
