from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    parse_error = "parse_error"
    static_validation_error = "static_validation_error"
    semantic_validation_error = "semantic_validation_error"
    permission_error = "permission_error"
    execution_error = "execution_error"
    cost_threshold_exceeded = "cost_threshold_exceeded"


class ErrorClassifier:
    def classify(self, validation_error: str) -> ErrorCategory:
        error = validation_error.lower()
        if "parse" in error:
            return ErrorCategory.parse_error
        if any(token in error for token in ("select_only", "no_select_star", "allowed_tables", "allowed_columns")):
            return ErrorCategory.static_validation_error
        if any(token in error for token in ("metric_column", "time_semantic", "allowed_dimensions", "aggregation")):
            return ErrorCategory.semantic_validation_error
        if "permission" in error or "granted" in error:
            return ErrorCategory.permission_error
        if any(token in error for token in ("timeout", "cost", "throttle")):
            return ErrorCategory.cost_threshold_exceeded
        return ErrorCategory.execution_error

    def should_repair(self, category: ErrorCategory) -> bool:
        return category in {ErrorCategory.semantic_validation_error, ErrorCategory.static_validation_error}


__all__ = ["ErrorCategory", "ErrorClassifier"]
