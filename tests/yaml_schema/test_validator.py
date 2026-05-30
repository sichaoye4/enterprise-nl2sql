from __future__ import annotations

from pathlib import Path

from src.semantic_registry.yaml_schema import validate_yaml_file
from tests.helpers import (
    valid_concept_yaml,
    valid_metric_yaml,
    valid_term_yaml,
    write_valid_semantic_tree,
    write_yaml,
)


def messages(errors) -> str:
    return "\n".join(error.message for error in errors)


def test_valid_yaml_file_parses_without_errors(tmp_yaml_path: Path) -> None:
    assert validate_yaml_file(tmp_yaml_path) == []


def test_missing_required_field_produces_validation_error(tmp_path: Path) -> None:
    root = write_valid_semantic_tree(tmp_path / "semantic")
    path = write_yaml(
        root / "terms" / "missing_owner.yaml",
        """
term: missing_owner
description: Missing owner.
domain: finance
""",
    )

    errors = validate_yaml_file(path)

    assert errors
    assert "Field required" in messages(errors)
    assert any(error.loc == ("owner",) for error in errors)


def test_invalid_enum_produces_validation_error(tmp_invalid_yaml_path: Path) -> None:
    errors = validate_yaml_file(tmp_invalid_yaml_path)

    assert errors
    assert "status" in {loc for error in errors for loc in error.loc}


def test_metric_concept_reference_must_exist(tmp_path: Path) -> None:
    root = write_valid_semantic_tree(tmp_path / "semantic")
    path = write_yaml(root / "metrics" / "broken_metric.yaml", valid_metric_yaml("broken_metric", "missing_concept"))

    errors = validate_yaml_file(path)

    assert any("references unknown concept 'missing_concept'" in error.message for error in errors)


def test_term_candidate_concepts_reference_must_exist(tmp_path: Path) -> None:
    root = write_valid_semantic_tree(tmp_path / "semantic")
    path = write_yaml(root / "terms" / "broken_term.yaml", valid_term_yaml("broken_term", "missing_concept"))

    errors = validate_yaml_file(path)

    assert any("references unknown concept 'missing_concept'" in error.message for error in errors)


def test_file_name_must_match_semantic_key(tmp_path: Path) -> None:
    root = write_valid_semantic_tree(tmp_path / "semantic")
    path = write_yaml(root / "terms" / "bar.yaml", valid_term_yaml("foo"))

    errors = validate_yaml_file(path)

    assert any("must match file name 'bar'" in error.message for error in errors)


def test_valid_single_concept_fixture_can_be_written(tmp_path: Path) -> None:
    root = write_valid_semantic_tree(tmp_path / "semantic")
    path = write_yaml(root / "concepts" / "another_concept.yaml", valid_concept_yaml("another_concept"))

    assert validate_yaml_file(path) == []
