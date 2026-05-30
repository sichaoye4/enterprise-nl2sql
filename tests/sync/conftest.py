from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import in_memory_engine, in_memory_session, write_valid_semantic_tree

__all__ = ["in_memory_engine", "in_memory_session", "tmp_semantic_dir"]


@pytest.fixture
def tmp_semantic_dir(tmp_path: Path) -> Path:
    return write_valid_semantic_tree(tmp_path / "semantic")
