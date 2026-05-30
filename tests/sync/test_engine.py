from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from src.semantic_registry.models import SemanticStatus, SemanticTerm
from src.semantic_registry.sync import sync_all
from tests.helpers import valid_term_yaml, write_yaml


@pytest.mark.asyncio
async def test_sync_is_idempotent(tmp_semantic_dir: Path, in_memory_session) -> None:
    first = await sync_all(session=in_memory_session, semantic_dir=tmp_semantic_dir)
    second = await sync_all(session=in_memory_session, semantic_dir=tmp_semantic_dir)

    assert first.created > 0
    assert first.errors == []
    assert second.created == 0
    assert second.updated == 0


@pytest.mark.asyncio
async def test_new_files_create_records(tmp_semantic_dir: Path, in_memory_session) -> None:
    await sync_all(session=in_memory_session, semantic_dir=tmp_semantic_dir)
    write_yaml(tmp_semantic_dir / "terms" / "revenue.yaml", valid_term_yaml("revenue"))

    report = await sync_all(session=in_memory_session, semantic_dir=tmp_semantic_dir)

    assert report.created == 1


@pytest.mark.asyncio
async def test_modified_files_update_records(tmp_semantic_dir: Path, in_memory_session) -> None:
    await sync_all(session=in_memory_session, semantic_dir=tmp_semantic_dir)
    write_yaml(
        tmp_semantic_dir / "terms" / "gmv.yaml",
        valid_term_yaml("gmv").replace("Gross merchandise value.", "Updated description."),
    )

    report = await sync_all(session=in_memory_session, semantic_dir=tmp_semantic_dir)
    row = (
        await in_memory_session.execute(select(SemanticTerm).where(SemanticTerm.term == "gmv"))
    ).scalar_one()

    assert report.updated > 0
    assert row.description == "Updated description."


@pytest.mark.asyncio
async def test_deleted_file_marks_record_as_deprecated(tmp_semantic_dir: Path, in_memory_session) -> None:
    await sync_all(session=in_memory_session, semantic_dir=tmp_semantic_dir)
    (tmp_semantic_dir / "terms" / "gmv.yaml").unlink()

    report = await sync_all(session=in_memory_session, semantic_dir=tmp_semantic_dir)
    row = (
        await in_memory_session.execute(select(SemanticTerm).where(SemanticTerm.term == "gmv"))
    ).scalar_one()

    assert report.deprecated > 0
    assert row.status == SemanticStatus.deprecated


@pytest.mark.asyncio
async def test_invalid_yaml_reports_error_without_crashing(tmp_semantic_dir: Path, in_memory_session) -> None:
    write_yaml(
        tmp_semantic_dir / "terms" / "bad.yaml",
        """
term: bad
description: Bad status.
owner: analytics
domain: finance
status: invalid_status
""",
    )

    report = await sync_all(session=in_memory_session, semantic_dir=tmp_semantic_dir)

    assert report.errors
    assert report.created > 0


@pytest.mark.asyncio
async def test_dry_run_rolls_back_without_creating_records(tmp_semantic_dir: Path, in_memory_session) -> None:
    report = await sync_all(session=in_memory_session, semantic_dir=tmp_semantic_dir, dry_run=True)
    rows = (await in_memory_session.execute(select(SemanticTerm))).scalars().all()

    assert report.created > 0
    assert rows == []
