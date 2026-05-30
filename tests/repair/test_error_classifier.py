from __future__ import annotations

from src.semantic_registry.repair import ErrorCategory, ErrorClassifier


def test_parse_error_classified_correctly() -> None:
    assert ErrorClassifier().classify("SQL parse failed near FROM") == ErrorCategory.parse_error


def test_static_validation_error_classified_correctly() -> None:
    assert ErrorClassifier().classify("static.allowed_columns: bad column") == ErrorCategory.static_validation_error


def test_semantic_validation_error_classified_correctly() -> None:
    assert ErrorClassifier().classify("semantic.metric_column_matches_plan: wrong metric") == ErrorCategory.semantic_validation_error


def test_permission_error_should_not_repair() -> None:
    classifier = ErrorClassifier()
    category = classifier.classify("permissions.granted: Permission denied")

    assert category == ErrorCategory.permission_error
    assert classifier.should_repair(category) is False


def test_semantic_error_should_repair() -> None:
    classifier = ErrorClassifier()
    category = classifier.classify("semantic.aggregation_matches_metric: wrong aggregation")

    assert classifier.should_repair(category) is True
