from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.semantic_registry import cli
from src.semantic_registry.sync.engine import SyncReport
from tests.helpers import write_valid_semantic_tree, write_yaml


def test_validate_command_with_valid_yaml_exits_zero(tmp_path: Path, capsys) -> None:
    root = write_valid_semantic_tree(tmp_path / "semantic")

    code = cli.main(["validate", str(root / "terms" / "gmv.yaml")])

    assert code == 0
    assert "Validation passed" in capsys.readouterr().out


def test_validate_command_with_invalid_yaml_exits_one(tmp_path: Path, capsys) -> None:
    root = write_valid_semantic_tree(tmp_path / "semantic")
    path = write_yaml(
        root / "terms" / "broken.yaml",
        """
term: broken
description: Broken.
domain: finance
""",
    )

    code = cli.main(["validate", str(path)])

    assert code == 1
    assert "Validation failed" in capsys.readouterr().err


def test_sync_command_prints_sync_report_json(capsys) -> None:
    session = MagicMock()
    sessionmaker = MagicMock()
    sessionmaker.return_value.__aenter__.return_value = session
    sessionmaker.return_value.__aexit__.return_value = None
    report = SyncReport(total=1, created=1)

    with (
        patch("src.semantic_registry.cli.get_sessionmaker", return_value=sessionmaker),
        patch("src.semantic_registry.cli.sync_all", new=AsyncMock(return_value=report)),
    ):
        code = cli.sync_command(argparse.Namespace(directory="semantic", dry_run=False))

    output = capsys.readouterr().out
    assert code == 0
    assert '"total": 1' in output
    assert '"created": 1' in output
