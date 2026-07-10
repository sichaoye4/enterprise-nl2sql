from __future__ import annotations

import uuid
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.semantic_registry.config import get_settings
from src.semantic_registry.metadata.models import ColumnMetadata, ExampleQuery, JoinPath, TableMetadata
from src.semantic_registry.metadata.provider import MetadataProvider
from src.semantic_registry.pipeline.candidate_generator import CandidateGenerator, SQLCandidate
from src.semantic_registry.pipeline.classifier import QuestionClassification, QuestionClassifier
from src.semantic_registry.pipeline.context_builder import ContextBuilder
from src.semantic_registry.pipeline.explainer import SQLExplainer, SQLExplanation
from src.semantic_registry.pipeline.response import PipelineResponse, ResponseBuilder
from src.semantic_registry.repair.repair_loop import RepairLoop
from src.semantic_registry.repair.selector import CandidateSelector
from src.semantic_registry.evaluation.pilot import PilotManager
from src.semantic_registry.resolver import (
    ClarificationBuilder,
    ClarificationResponse,
    ExtractedTerm,
    SemanticQueryPlan,
    SemanticResolver,
    load_semantic_registry,
)
from src.semantic_registry.resolver.registry import SemanticRegistryData
from src.semantic_registry.retrieval.hybrid import HybridRetriever, RetrievalResult
from src.semantic_registry.validation.orchestrator import SQLValidator, ValidationSuiteResult
from src.semantic_registry.yaml_schema.schemas import DimensionYaml, MetricYaml


class PipelineContext(BaseModel):
    query_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    question: str
    domain: str | None = None
    user: str | None = None
    classification: QuestionClassification | None = None
    extracted_terms: list[ExtractedTerm] = Field(default_factory=list)
    semantic_plan: SemanticQueryPlan | None = None
    retrieved_metadata: RetrievalResult | None = None
    context_prompt: str | None = None
    sql_candidates: list[SQLCandidate] = Field(default_factory=list)
    selected_sql: SQLCandidate | None = None
    validation_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    selection_log: list[dict[str, Any]] = Field(default_factory=list)
    explanation: SQLExplanation | None = None
    response: PipelineResponse | None = None
    requires_clarification: bool = False
    clarification: ClarificationResponse | None = None
    error: str | None = None
    semantic_route: str | None = None
    semantic_compiled_sql: str | None = None
    guardrail_contract: dict[str, Any] | None = None
    gap_report: dict[str, Any] | None = None
    trace: list[str] = Field(default_factory=list)


class RegistryMetadataProvider(MetadataProvider):
    def __init__(self, registry_data: SemanticRegistryData) -> None:
        self.registry_data = registry_data
        self.concepts_by_name = {concept.concept: concept for concept in registry_data.concepts}
        self.dimensions_by_name = {dimension.dimension: dimension for dimension in registry_data.dimensions}
        self.join_paths = [
            JoinPath(
                from_table=join.from_table,
                to_table=join.to_table,
                relationship=join.relationship,
                join_condition=join.join_condition,
                safe_for_metrics=join.safe_for_metrics,
                fanout_risk=join.fanout_risk,
            )
            for join in registry_data.join_paths
        ]
        self.tables = self._build_tables(registry_data.metrics)

    def search_tables(self, query: str, domain: str | None = None) -> list[TableMetadata]:
        query_terms = {part.lower() for part in query.replace("_", " ").split() if part}
        tables = self.list_tables(domain=domain)
        if not query_terms:
            return tables
        matching = [
            table
            for table in tables
            if query_terms & {part.lower() for part in f"{table.table_name} {table.description}".replace("_", " ").split()}
        ]
        return matching or tables

    def get_table(self, table_name: str) -> TableMetadata | None:
        return self.tables.get(table_name)

    def get_columns(self, table_name: str) -> list[ColumnMetadata]:
        table = self.get_table(table_name)
        return table.columns if table else []

    def get_join_paths(self, tables: list[str]) -> list[JoinPath]:
        table_names = set(tables)
        return [join for join in self.join_paths if join.from_table in table_names or join.to_table in table_names]

    def get_example_queries(self, query: str) -> list[ExampleQuery]:
        return []

    def list_tables(self, domain: str | None = None) -> list[TableMetadata]:
        tables = list(self.tables.values())
        if domain is None:
            return tables
        return [table for table in tables if table.domain in (None, "", domain)]

    def _build_tables(self, metrics: list[MetricYaml]) -> dict[str, TableMetadata]:
        by_table: dict[str, TableMetadata] = {}
        for metric in metrics:
            if metric.measure is None:
                continue
            table = by_table.setdefault(
                metric.measure.table,
                TableMetadata(
                    table_name=metric.measure.table,
                    description="Certified semantic metric source.",
                    domain=self._metric_domain(metric),
                    certified=True,
                    eligible_for_nl2sql=True,
                    columns=[],
                    usage_popularity=0.8,
                ),
            )
            if self._metric_domain(metric) and table.domain is None:
                table.domain = self._metric_domain(metric)
            self._add_column(
                table,
                ColumnMetadata(
                    column_name=metric.measure.column,
                    data_type="numeric",
                    description=f"Measure for {self._business_name(metric.metric)}.",
                    concept=metric.metric,
                    aggregation=metric.aggregation,
                    unit=metric.unit,
                ),
            )
            if metric.physical_time_column:
                self._add_column(
                    table,
                    ColumnMetadata(
                        column_name=metric.physical_time_column,
                        data_type="date",
                        description=f"Default time column for {self._business_name(metric.metric)}.",
                        concept=metric.default_time_dimension,
                    ),
                )
                table.partition_column = table.partition_column or metric.physical_time_column
            for dimension_name in metric.allowed_dimensions:
                dimension = self.dimensions_by_name.get(dimension_name)
                mapping = self._dimension_mapping_for_table(dimension, metric.measure.table) if dimension else None
                if mapping:
                    self._add_column(
                        table,
                        ColumnMetadata(
                            column_name=mapping.column,
                            data_type="text",
                            description=dimension.description,
                            concept=dimension.dimension,
                        ),
                    )
            table.description = self._table_description(table, metric)
            table.grain = [column.column_name for column in table.columns if column.concept != metric.metric]
        for join in self.join_paths:
            if join.from_table in by_table and all(existing.join_condition != join.join_condition for existing in by_table[join.from_table].join_paths):
                by_table[join.from_table].join_paths.append(join)
        return by_table

    def _add_column(self, table: TableMetadata, column: ColumnMetadata) -> None:
        if all(existing.column_name != column.column_name for existing in table.columns):
            table.columns.append(column)

    def _dimension_mapping_for_table(self, dimension: DimensionYaml | None, table_name: str) -> Any:
        if dimension is None:
            return None
        for mapping in dimension.physical_mappings:
            if mapping.table == table_name:
                return mapping
        return None

    def _metric_domain(self, metric: MetricYaml) -> str | None:
        concept = self.concepts_by_name.get(metric.concept)
        return concept.domain if concept else None

    def _table_description(self, table: TableMetadata, metric: MetricYaml) -> str:
        metric_names = sorted(
            {
                self._business_name(column.concept)
                for column in table.columns
                if column.aggregation and column.concept
            }
        )
        metrics_text = ", ".join(name for name in metric_names if name) or self._business_name(metric.metric)
        return f"Certified semantic source for {metrics_text}."

    def _business_name(self, value: str | None) -> str:
        if not value:
            return ""
        return " ".join(part.upper() if part.lower() in {"gmv", "pii", "id"} else part.capitalize() for part in value.split("_"))


class NL2SQLPipeline:
    def __init__(
        self,
        *,
        semantic_dir: str | Path | None = None,
        registry_data: SemanticRegistryData | None = None,
        classifier: QuestionClassifier | None = None,
        resolver: SemanticResolver | None = None,
        retriever: HybridRetriever | None = None,
        metadata_provider: MetadataProvider | None = None,
        context_builder: ContextBuilder | None = None,
        candidate_generator: CandidateGenerator | None = None,
        sql_validator: SQLValidator | None = None,
        repair_loop: RepairLoop | None = None,
        selector: CandidateSelector | None = None,
        explainer: SQLExplainer | None = None,
        response_builder: ResponseBuilder | None = None,
        semantic_engine: Any | None = None,
        semantic_model_path: str | Path | None = None,
    ) -> None:
        self.registry_data = registry_data or load_semantic_registry(semantic_dir or get_settings().semantic_dir)
        self.metadata_provider = metadata_provider or RegistryMetadataProvider(self.registry_data)
        self.classifier = classifier or QuestionClassifier()
        self.resolver = resolver or SemanticResolver(self.registry_data)
        self.retriever = retriever or HybridRetriever(
            embedding_service=None,
            metadata_provider=self.metadata_provider,
            semantic_registry_data=self.registry_data.as_retrieval_data(),
        )
        self.context_builder = context_builder or ContextBuilder(self.registry_data, self.metadata_provider)
        self.candidate_generator = candidate_generator or CandidateGenerator()
        self.sql_validator = sql_validator or SQLValidator()
        self.repair_loop = repair_loop or RepairLoop(metadata_provider=self.metadata_provider)
        self.selector = selector or CandidateSelector()
        self.explainer = explainer or SQLExplainer()
        self.response_builder = response_builder or ResponseBuilder()
        self.clarification_builder = ClarificationBuilder()
        self.semantic_model_path = Path(semantic_model_path) if semantic_model_path else self._default_semantic_model_path()
        self.semantic_engine = semantic_engine
        self._semantic_engine_cache: dict[Path, Any] = {}

    def run(self, question: str, domain: str | None = None, user: str | None = None) -> PipelineContext:
        context = PipelineContext(question=question, domain=domain, user=user)
        pilot = PilotManager()
        if user is not None and not pilot.is_pilot_user(user):
            context.error = "User is not enabled for the NL2SQL pilot."
            return self.build_response(context)
        if user is not None and not pilot.is_domain_allowed(user, domain):
            context.error = "User is not enabled for this NL2SQL pilot domain."
            return self.build_response(context)
        stages = [
            self.classify,
            self.extract_terms,
            self.resolve_semantics,
            self.run_semantic_engine,
            self.retrieve_metadata,
            self.build_context,
            self.generate_candidates,
            self.validate,
            self.repair,
            self.select,
            self.explain,
            self.build_response,
        ]
        for stage in stages:
            if self._should_skip_stage(stage.__name__, context):
                continue
            context = stage(context)
            if stage.__name__ == "build_response":
                continue
            if context.requires_clarification or context.error:
                context = self.build_response(context)
                break
        return context

    def _should_skip_stage(self, stage_name: str, context: PipelineContext) -> bool:
        if context.semantic_route == "SEMANTIC_SQL" and stage_name in {"generate_candidates", "validate", "repair"}:
            return True
        return False

    def classify(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("classify")
        classification = self.classifier.classify(context.question)
        context.classification = classification
        context.domain = context.domain or classification.domain
        if classification.write_intent:
            context.error = "Write intent detected; only read-only SELECT questions are supported."
        elif classification.sensitive_data_intent:
            context.error = "Sensitive data intent detected; PII fields are not available through NL2SQL."
        return context

    def extract_terms(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("extract_terms")
        context.extracted_terms = self.resolver.extractor.extract(context.question)
        return context

    def resolve_semantics(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("resolve_semantics")
        try:
            plan = self.resolver.resolve(context.question, domain=context.domain)
        except Exception as exc:
            context.error = f"Semantic resolution failed: {exc}"
            return context
        if self._can_continue_through_dimension_term_ambiguity(context, plan):
            plan.requires_clarification = False
            plan.clarification_question = None
        context.semantic_plan = plan
        context.domain = context.domain or plan.domain
        if plan.requires_clarification:
            context.requires_clarification = True
            context.clarification = self._clarification(context)
        return context

    def run_semantic_engine(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("run_semantic_engine")
        try:
            result = self._semantic_pipeline(context.domain).process(context.question)
        except Exception as exc:
            context.error = f"Semantic engine failed: {exc}"
            return context

        route = self._result_value(result, "route")
        context.semantic_route = str(route) if route else None
        context.gap_report = self._model_dump(self._result_value(result, "gap_report"))

        if context.semantic_route == "SEMANTIC_SQL":
            compiled_query = self._result_value(result, "compiled_query")
            compiled_sql = self._result_value(compiled_query, "sql")
            if not compiled_sql:
                context.error = "Semantic engine selected SEMANTIC_SQL but returned no compiled SQL."
                return context
            context.semantic_compiled_sql = str(compiled_sql)
            candidate = self._semantic_sql_candidate(str(compiled_sql), compiled_query)
            context.sql_candidates = [candidate]
            context.selected_sql = candidate
        elif context.semantic_route == "GUARDED_LLM_SQL":
            contract = self._result_value(result, "guardrail_contract")
            context.guardrail_contract = self._model_dump(contract)
        elif context.semantic_route == "CLARIFY":
            # Log gap report but don't short-circuit — let the pipeline fall
            # through to the existing LLM stages which may still produce SQL.
            pass
        elif context.semantic_route == "BLOCKED":
            context.error = self._semantic_gap_message("Semantic engine blocked this question.", context.gap_report)
        elif context.semantic_route:
            context.error = f"Semantic engine returned unsupported route: {context.semantic_route}"
        return context

    def _can_continue_through_dimension_term_ambiguity(
        self,
        context: PipelineContext,
        plan: SemanticQueryPlan,
    ) -> bool:
        if not plan.requires_clarification or not plan.metric or not plan.dimension or not plan.clarification_question:
            return False
        extracted_metric_terms = {term.term for term in context.extracted_terms}
        if plan.metric not in extracted_metric_terms:
            return False
        question = plan.clarification_question.lower()
        dimension = plan.dimension.lower()
        return f"'{dimension}'" in question or dimension.replace("_", " ") in question

    def retrieve_metadata(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("retrieve_metadata")
        context.retrieved_metadata = self.retriever.retrieve(context.question, domain=context.domain, top_k=5)
        return context

    def build_context(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("build_context")
        if context.semantic_plan is None or context.retrieved_metadata is None:
            context.error = "Cannot build context without semantic plan and retrieved metadata."
            return context
        context.context_prompt = self.context_builder.build(context.question, context.semantic_plan, context.retrieved_metadata)
        if context.semantic_route == "GUARDED_LLM_SQL" and context.guardrail_contract:
            context.context_prompt = self._inject_guardrail_contract(context.context_prompt, context.guardrail_contract)
        return context

    def generate_candidates(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("generate_candidates")
        context.sql_candidates = self.candidate_generator.generate_candidates(context)
        return context

    def validate(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("validate")
        if not context.sql_candidates:
            context.error = "No SQL candidates were generated."
            return context
        if context.semantic_plan is None:
            context.error = "Cannot validate SQL without a semantic plan."
            return context
        allowed_tables, allowed_columns = self._allowed_metadata(context)
        for candidate in context.sql_candidates:
            result = self.sql_validator.validate(
                candidate.sql,
                context.semantic_plan,
                self.metadata_provider,
                "system",
                allowed_tables,
                allowed_columns,
            )
            candidate.parse_success = not self._has_parse_error(result)
            candidate.validation_errors = self._validation_errors(result)
            candidate.validation_results = result.model_dump(mode="json")
            context.validation_results[candidate.candidate_id] = candidate.validation_results
        return context

    def repair(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("repair")
        self.repair_loop.repair(context, self.sql_validator)
        return context

    def select(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("select")
        context.selected_sql = self.selector.select(context.sql_candidates)
        context.selection_log = list(self.selector.selection_log)
        return context

    def explain(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("explain")
        if context.semantic_plan is None or context.selected_sql is None:
            context.error = "Cannot explain SQL without a semantic plan and selected SQL."
            return context
        context.explanation = self.explainer.explain(context.semantic_plan, context.selected_sql)
        return context

    def build_response(self, context: PipelineContext) -> PipelineContext:
        if not context.trace or context.trace[-1] != "build_response":
            context.trace.append("build_response")
        context.response = self.response_builder.build(context)
        return context

    def _allowed_metadata(self, context: PipelineContext) -> tuple[set[str], dict[str, set[str]]]:
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

    def _clarification(self, context: PipelineContext) -> ClarificationResponse:
        try:
            clarification = self.resolver.build_clarification(context.question, context={"domain": context.domain})
        except Exception:
            clarification = ClarificationResponse(needs_clarification=False, message="", options=[])
        if clarification.needs_clarification:
            return clarification
        question = context.semantic_plan.clarification_question if context.semantic_plan else None
        return ClarificationResponse(
            needs_clarification=True,
            message=question or "Can you clarify the metric, dimension, or business domain?",
            options=[],
        )

    def _semantic_pipeline(self, domain: str | None = None) -> Any:
        if self.semantic_engine is not None:
            return self.semantic_engine
        try:
            from semantic_engine.pipeline import SemanticPipeline
        except ModuleNotFoundError:
            source_path = Path.home() / "semantic_modeling" / "src"
            if str(source_path) not in sys.path:
                sys.path.insert(0, str(source_path))
            from semantic_engine.pipeline import SemanticPipeline

        model_path = self._semantic_model_path_for_domain(domain)
        if model_path not in self._semantic_engine_cache:
            self._semantic_engine_cache[model_path] = SemanticPipeline(model_path)
        return self._semantic_engine_cache[model_path]

    def _semantic_model_path_for_domain(self, domain: str | None = None) -> Path:
        if not domain or self.semantic_model_path.is_file():
            return self.semantic_model_path
        model_file = self.semantic_model_path / domain / "model.yml"
        if model_file.exists():
            return model_file
        domain_dir = self.semantic_model_path / domain
        if domain_dir.exists():
            return domain_dir
        return self.semantic_model_path

    def _default_semantic_model_path(self) -> Path:
        root = Path.home() / "semantic_modeling" / "semantic_models"
        commerce = root / "commerce"
        return commerce if commerce.exists() else root

    def _semantic_sql_candidate(self, sql: str, compiled_query: Any) -> SQLCandidate:
        lineage = self._model_dump(self._result_value(compiled_query, "lineage")) or {}
        columns = self._lineage_columns(lineage)
        return SQLCandidate(
            candidate_id="semantic_engine",
            sql=sql,
            generation_strategy="semantic_engine",
            assumptions=["Compiled deterministically by the governed semantic engine."],
            tables_used=list(lineage.get("tables") or []),
            columns_used=columns,
            confidence="high",
            reasoning_summary="Semantic engine compiled SQL from fully governed semantic coverage.",
            parse_success=True,
            validation_errors=[],
        )

    def _lineage_columns(self, lineage: dict[str, Any]) -> list[str]:
        columns: list[str] = []
        for key in ("measures", "filters"):
            value = lineage.get(key)
            if isinstance(value, dict):
                for item in value.values():
                    if isinstance(item, dict):
                        column = item.get("column") or item.get("expr") or item.get("expression")
                        if isinstance(column, str):
                            columns.append(column)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        column = item.get("column") or item.get("expr") or item.get("expression")
                        if isinstance(column, str):
                            columns.append(column)
        return list(dict.fromkeys(columns))

    def _inject_guardrail_contract(self, prompt: str, contract: dict[str, Any]) -> str:
        contract_text = json.dumps(contract, sort_keys=True)
        return "\n\n".join(
            [
                prompt,
                "GuardrailContract:",
                "The SQL must use only the tables, columns, measures, dimensions, time dimensions, joins, segments, and invariants in this contract.",
                "<guardrail_contract>\n" + contract_text + "\n</guardrail_contract>",
            ]
        )

    def _semantic_clarification(self, context: PipelineContext) -> ClarificationResponse:
        message = self._semantic_gap_message("Can you clarify the governed semantic intent?", context.gap_report)
        return ClarificationResponse(needs_clarification=True, message=message, options=[])

    def _semantic_gap_message(self, fallback: str, gap_report: dict[str, Any] | None) -> str:
        if not gap_report:
            return fallback
        parts: list[str] = []
        unresolved = gap_report.get("unresolved_terms") or []
        missing = gap_report.get("missing_members") or []
        missing_measures = gap_report.get("missing_measures") or []
        missing_dimensions = gap_report.get("missing_dimensions") or []
        if unresolved:
            parts.append("unresolved terms: " + ", ".join(str(item) for item in unresolved))
        if missing_measures:
            parts.append("missing measures: " + ", ".join(str(item) for item in missing_measures))
        if missing_dimensions:
            parts.append("missing dimensions: " + ", ".join(str(item) for item in missing_dimensions))
        if missing:
            parts.append("missing members: " + ", ".join(str(item) for item in missing))
        if not parts:
            suggestions = gap_report.get("suggested_model_additions") or []
            if suggestions:
                parts.append("suggested additions: " + ", ".join(str(item) for item in suggestions))
        return f"{fallback} " + "; ".join(parts) if parts else fallback

    def _model_dump(self, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json", exclude_none=True)
        return None

    def _result_value(self, value: Any, key: str) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            return value.get(key)
        return getattr(value, key, None)


__all__ = ["NL2SQLPipeline", "PipelineContext", "RegistryMetadataProvider"]
