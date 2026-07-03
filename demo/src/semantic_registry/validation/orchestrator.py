from __future__ import annotations

from pydantic import BaseModel

from src.semantic_registry.metadata.models import TableMetadata
from src.semantic_registry.metadata.provider import MetadataProvider
from src.semantic_registry.resolver.plan import SemanticQueryPlan
from src.semantic_registry.validation.limit import inject_limit
from src.semantic_registry.validation.parser import extract_columns, extract_tables, parse_sql
from src.semantic_registry.validation.partition_checker import PartitionCheckResult, PartitionFilterChecker
from src.semantic_registry.validation.permissions import AllowAllPermissionChecker, PermissionChecker, PermissionResult
from src.semantic_registry.validation.semantic_validator import (
    SemanticCheckResult,
    SemanticValidationResult,
    SemanticValidator,
)
from src.semantic_registry.validation.static_validator import CheckResult, StaticValidator, ValidationResult


class ValidationSuiteResult(BaseModel):
    passed: bool
    static: ValidationResult
    semantic: SemanticValidationResult
    permissions: PermissionResult
    partition: PartitionCheckResult
    modified_sql: str | None = None


class SQLValidator:
    def __init__(
        self,
        *,
        static_validator: StaticValidator | None = None,
        semantic_validator: SemanticValidator | None = None,
        permission_checker: PermissionChecker | None = None,
        partition_checker: PartitionFilterChecker | None = None,
        stop_on_first_failure: bool = False,
        preview_limit: int = 100,
    ) -> None:
        self.static_validator = static_validator or StaticValidator(require_limit=False)
        self.semantic_validator = semantic_validator or SemanticValidator()
        self.permission_checker = permission_checker or AllowAllPermissionChecker()
        self.partition_checker = partition_checker or PartitionFilterChecker()
        self.stop_on_first_failure = stop_on_first_failure
        self.preview_limit = preview_limit

    def validate(
        self,
        sql: str,
        semantic_plan: SemanticQueryPlan,
        metadata_provider: MetadataProvider,
        user: str,
        allowed_tables: set[str],
        allowed_columns: dict[str, set[str]],
    ) -> ValidationSuiteResult:
        parsed_tables: list[str] = []
        parsed_columns: list[str] = []
        try:
            statement = parse_sql(sql)
            parsed_tables = extract_tables(statement)
            parsed_columns = extract_columns(statement)
        except Exception:
            pass

        static = self.static_validator.validate(sql, allowed_tables, allowed_columns)
        if self.stop_on_first_failure and not static.passed:
            return self._result(static=static)

        semantic = self.semantic_validator.validate(sql, semantic_plan, metadata_provider)
        if self.stop_on_first_failure and not semantic.passed:
            return self._result(static=static, semantic=semantic)

        permissions = self.permission_checker.check_permissions(user, sql, parsed_tables, parsed_columns)
        if self.stop_on_first_failure and not permissions.granted:
            return self._result(static=static, semantic=semantic, permissions=permissions)

        partition_tables = self._metadata_tables(metadata_provider, parsed_tables)
        partition = self.partition_checker.check(sql, partition_tables)
        if self.stop_on_first_failure and not partition.passed:
            return self._result(static=static, semantic=semantic, permissions=permissions, partition=partition)

        modified_sql = inject_limit(sql, self.preview_limit) if static.passed and semantic.passed and permissions.granted else None
        return ValidationSuiteResult(
            passed=static.passed and semantic.passed and permissions.granted and partition.passed,
            static=static,
            semantic=semantic,
            permissions=permissions,
            partition=partition,
            modified_sql=modified_sql,
        )

    def _metadata_tables(self, metadata_provider: MetadataProvider, table_names: list[str]) -> list[TableMetadata]:
        tables: list[TableMetadata] = []
        for table_name in table_names:
            table = metadata_provider.get_table(table_name) or metadata_provider.get_table(table_name.rsplit(".", 1)[-1])
            if table is not None:
                tables.append(table)
        return tables

    def _result(
        self,
        *,
        static: ValidationResult,
        semantic: SemanticValidationResult | None = None,
        permissions: PermissionResult | None = None,
        partition: PartitionCheckResult | None = None,
    ) -> ValidationSuiteResult:
        semantic = semantic or SemanticValidationResult(
            passed=False,
            checks=[SemanticCheckResult(name="skipped", passed=False, message="Skipped after earlier failure.")],
            errors=["Skipped after earlier failure."],
        )
        permissions = permissions or PermissionResult(granted=False, message="Skipped after earlier failure.")
        partition = partition or PartitionCheckResult(passed=False, warnings=["Skipped after earlier failure."])
        return ValidationSuiteResult(
            passed=False,
            static=static,
            semantic=semantic,
            permissions=permissions,
            partition=partition,
            modified_sql=None,
        )
