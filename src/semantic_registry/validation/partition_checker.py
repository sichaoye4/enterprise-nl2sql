from __future__ import annotations

import os

from pydantic import BaseModel, Field
from sqlglot import exp

from src.semantic_registry.metadata.models import TableMetadata
from src.semantic_registry.validation.parser import parse_sql


class PartitionCheckResult(BaseModel):
    passed: bool
    missing_filters: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PartitionFilterChecker:
    def __init__(self, *, dialect: str | None = None, row_threshold: int = 10_000_000) -> None:
        self.dialect = dialect or os.getenv("SQL_DIALECT", "spark")
        self.row_threshold = row_threshold

    def check(self, sql: str, tables: list[TableMetadata]) -> PartitionCheckResult:
        try:
            statement = parse_sql(sql, dialect=self.dialect)
        except Exception as exc:
            return PartitionCheckResult(passed=False, warnings=[f"Could not parse SQL for partition checks: {exc}"])

        filtered_columns = self._filtered_columns(statement)
        missing_filters: list[str] = []
        warnings: list[str] = []

        for table in tables:
            if not table.partition_column:
                if self._is_large_uncertified(table):
                    warnings.append(f"{table.table_name} is large and uncertified but has no documented partition column.")
                continue
            if table.partition_column not in filtered_columns:
                missing_filters.append(f"{table.table_name}.{table.partition_column}")

        return PartitionCheckResult(passed=not missing_filters, missing_filters=missing_filters, warnings=warnings)

    def _filtered_columns(self, statement: exp.Expression) -> set[str]:
        where = statement.args.get("where")
        if where is None:
            return set()
        return {column.name for column in where.find_all(exp.Column)}

    def _is_large_uncertified(self, table: TableMetadata) -> bool:
        row_count = getattr(table, "row_count", 0) or getattr(table, "estimated_rows", 0) or 0
        return row_count > self.row_threshold and not table.certified
