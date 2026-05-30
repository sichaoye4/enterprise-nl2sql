from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import write_valid_semantic_tree, write_yaml


@pytest.fixture
def tmp_yaml_path(tmp_path: Path) -> Path:
    root = write_valid_semantic_tree(tmp_path / "semantic")
    return root / "terms" / "gmv.yaml"


@pytest.fixture
def tmp_invalid_yaml_path(tmp_path: Path) -> Path:
    root = write_valid_semantic_tree(tmp_path / "semantic")
    return write_yaml(
        root / "terms" / "broken.yaml",
        """
term: broken
description: Missing owner.
domain: finance
status: invalid_status
""",
    )
