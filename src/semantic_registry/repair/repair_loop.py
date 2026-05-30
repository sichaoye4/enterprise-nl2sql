from __future__ import annotations

from typing import TYPE_CHECKING

from src.semantic_registry.metadata.provider import MetadataProvider
from src.semantic_registry.validation.orchestrator import SQLValidator, ValidationSuiteResult

from .error_classifier import ErrorClassifier

if TYPE_CHECKING:
    from src.semantic_registry.pipeline.candidate_generator import SQLCandidate
    from src.semantic_registry.pipeline.llm_gateway import LLMGateway
    from src.semantic_registry.pipeline.state_machine import PipelineContext


class RepairLoop:
    def __init__(
        self,
        *,
        metadata_provider: MetadataProvider,
        llm_gateway: "LLMGateway | None" = None,
        user: str = "system",
    ) -> None:
        if llm_gateway is None:
            from src.semantic_registry.pipeline.llm_gateway import LLMGateway

            llm_gateway = LLMGateway()
        self.metadata_provider = metadata_provider
        self.llm_gateway = llm_gateway
        self.user = user
        self.classifier = ErrorClassifier()

    def repair(self, context: "PipelineContext", sql_validator: SQLValidator) -> list[SQLCandidate]:
        repaired: list[SQLCandidate] = []
        if context.semantic_plan is None:
            return repaired

        allowed_tables, allowed_columns = self._allowed_metadata(context)
        for candidate in context.sql_candidates:
            if candidate.parse_success and not candidate.validation_errors:
                continue
            repairable_errors = [
                error
                for error in candidate.validation_errors
                if self.classifier.should_repair(self.classifier.classify(error))
            ]
            if not repairable_errors:
                continue

            response = self.llm_gateway.generate(self.repair_prompt(candidate, repairable_errors, context))
            result = sql_validator.validate(
                response.sql,
                context.semantic_plan,
                self.metadata_provider,
                self.user,
                allowed_tables,
                allowed_columns,
            )
            candidate.sql = response.sql
            candidate.assumptions = response.assumptions
            candidate.tables_used = response.tables_used
            candidate.columns_used = response.columns_used
            candidate.confidence = response.confidence
            candidate.reasoning_summary = response.reasoning_summary
            candidate.parse_success = not self._has_parse_error(result)
            candidate.validation_errors = self._validation_errors(result)
            candidate.repair_attempted = True
            candidate.repaired = result.passed
            context.validation_results[candidate.candidate_id] = result.model_dump(mode="json")
            if result.passed:
                repaired.append(candidate)
        return repaired

    def repair_prompt(
        self,
        candidate: SQLCandidate,
        validation_errors: list[str],
        context: "PipelineContext | None" = None,
    ) -> str:
        semantic_plan = context.semantic_plan.model_dump(mode="json") if context and context.semantic_plan else {}
        prompt_parts = [
            "Repair this SQL candidate using the governed semantic plan.",
            "Return only the JSON SQL generation contract.",
            f"Original SQL:\n{candidate.sql}",
            "Validation errors:\n" + "\n".join(f"- {error}" for error in validation_errors),
            f"Semantic plan context:\n{semantic_plan}",
        ]
        if context and context.context_prompt:
            prompt_parts.append(context.context_prompt)
        return "\n\n".join(prompt_parts)

    def _allowed_metadata(self, context: "PipelineContext") -> tuple[set[str], dict[str, set[str]]]:
        tables = self.metadata_provider.list_tables(domain=context.domain)
        allowed_tables = {table.table_name for table in tables}
        allowed_columns = {
            table.table_name: {column.column_name for column in table.columns}
            for table in tables
        }
        return allowed_tables, allowed_columns

    def _validation_errors(self, result: ValidationSuiteResult) -> list[str]:
        errors: list[str] = []
        errors.extend(self._failed_check_messages("static", result.static.checks))
        errors.extend(self._failed_check_messages("semantic", result.semantic.checks))
        if not result.permissions.granted:
            errors.append(f"permissions.granted: {result.permissions.message or 'Permission denied.'}")
        if not result.partition.passed:
            errors.extend(f"partition: {warning}" for warning in result.partition.warnings)
            errors.extend(f"partition.missing_filter: {missing}" for missing in result.partition.missing_filters)
        return errors

    def _failed_check_messages(self, prefix: str, checks: list) -> list[str]:
        return [f"{prefix}.{check.name}: {check.message}" for check in checks if not check.passed]

    def _has_parse_error(self, result: ValidationSuiteResult) -> bool:
        return any(check.name == "parse" and not check.passed for check in result.static.checks)


__all__ = ["RepairLoop"]
