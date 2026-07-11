from __future__ import annotations

import uuid
import json
import logging
import sys
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from pydantic import BaseModel, Field

from src.semantic_registry.config import get_settings
from src.semantic_registry.metadata.models import ColumnMetadata, ExampleQuery, JoinPath, TableMetadata
from src.semantic_registry.metadata.provider import MetadataProvider
from src.semantic_registry.pipeline.candidate_generator import CandidateGenerator, SQLCandidate
from src.semantic_registry.pipeline.classifier import QuestionClassification, QuestionClassifier
from src.semantic_registry.pipeline.context_builder import ContextBuilder
from src.semantic_registry.pipeline.explainer import SQLExplainer, SQLExplanation
from src.semantic_registry.pipeline.response import PipelineResponse, ResponseBuilder
from src.semantic_registry.pipeline.semantic_judge import LLMJudge
from src.semantic_registry.pipeline.semantic_router import SemanticRouter, compile_from_router
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
from src.semantic_registry.retrieval.hybrid import HybridRetriever, RetrievalResult, ScoredCandidate
from src.semantic_registry.validation.orchestrator import SQLValidator, ValidationSuiteResult
from src.semantic_registry.yaml_schema.schemas import DimensionYaml, MetricYaml


logger = logging.getLogger(__name__)


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
    semantic_result: dict[str, Any] | None = None
    semantic_context: dict[str, Any] | None = None
    guardrail_contract: dict[str, Any] | None = None
    semantic_retry_count: int = 0
    llm_judge_retry_count: int = 0
    llm_judge_result: dict[str, Any] | None = None
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
        llm_judge: Any | None = None,
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
        self.llm_judge = llm_judge or LLMJudge()
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
            self.run_semantic_engine,
            self.run_semantic_quality_gate,
            self.run_semantic_llm_router,
            self.extract_terms,
            self.resolve_semantics,
            self.retrieve_metadata,
            self.build_context,
            self.generate_candidates,
            self.validate,
            self.repair,
            self.select,
            self.run_llm_judge,
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
        if context.semantic_route == "SEMANTIC_SQL" and stage_name in {
            "extract_terms",
            "resolve_semantics",
            "retrieve_metadata",
            "build_context",
            "generate_candidates",
        }:
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
        context.semantic_result = self._model_dump(result)
        context.semantic_context = self._model_dump(self._result_value(result, "semantic_context"))
        context.gap_report = self._model_dump(self._result_value(result, "gap_report"))

        if context.semantic_route == "SEMANTIC_SQL":
            compiled_query = self._result_value(result, "compiled_candidate") or self._result_value(result, "compiled_query")
            compiled_sql = self._result_value(compiled_query, "sql")
            if not compiled_sql:
                context.error = "Semantic engine selected SEMANTIC_SQL but returned no compiled SQL."
                return context
            context.semantic_compiled_sql = str(compiled_sql)
            context.guardrail_contract = self._semantic_guardrail_contract(result, compiled_query)
            context.semantic_plan = self._semantic_plan_from_engine_result(result, compiled_query, context)
            context.retrieved_metadata = self._semantic_retrieval_context(context.semantic_plan, compiled_query)
            context.sql_candidates = [self._semantic_sql_candidate(context.semantic_compiled_sql, compiled_query)]
            context.selected_sql = None
        elif context.semantic_route in {"SEMANTIC_ASSISTED_LLM", "GUARDED_LLM_SQL"}:
            context.semantic_route = "SEMANTIC_ASSISTED_LLM"
            contract = self._result_value(result, "guardrail_contract")
            context.guardrail_contract = self._model_dump(contract)
        elif context.semantic_route == "CLARIFY":
            # Log gap report but don't short-circuit — let the pipeline fall
            # through to the existing LLM stages which may still produce SQL.
            context.requires_clarification = True
            context.clarification = self._semantic_clarification(context)
        elif context.semantic_route in {"REJECTED", "BLOCKED"}:
            context.error = self._semantic_gap_message("Semantic engine rejected this question.", context.gap_report)
        elif context.semantic_route == "BASELINE_LLM":
            pass
        elif context.semantic_route:
            context.error = f"Semantic engine returned unsupported route: {context.semantic_route}"
        return context

    def run_semantic_quality_gate(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("run_semantic_quality_gate")
        if context.semantic_route != "SEMANTIC_SQL" or not context.semantic_compiled_sql:
            return context

        orphan_filters = self._semantic_quality_gate(context.semantic_compiled_sql, context.question)
        if not orphan_filters:
            return context

        logger.warning("Semantic SQL quality gate failed; orphan filters detected: %s", orphan_filters)
        context.gap_report = dict(context.gap_report or {})
        context.gap_report["quality_gate"] = {
            "status": "failed",
            "orphan_filters": orphan_filters,
        }
        context.semantic_route = "SEMANTIC_ASSISTED_LLM"
        context.semantic_compiled_sql = None
        context.sql_candidates = []
        context.selected_sql = None
        return context

    def run_semantic_llm_router(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("run_semantic_llm_router")
        if context.semantic_route not in ("SEMANTIC_ASSISTED_LLM", "BASELINE_LLM", None):
            return context
        try:
            snapshot = self._load_semantic_snapshot(context.domain)
            if snapshot is None:
                return context
            self._build_catalog_listing(snapshot)
            router = SemanticRouter(snapshot, self._llm_router_generate)
            result = router.route(context.question, db_id=context.domain)
            if result is None:
                return context
            compiled = compile_from_router(snapshot, result, context.question)
            if compiled is None:
                return context
            orphan_filters = self._semantic_quality_gate(compiled.sql, context.question, compiled.parameters)
            if orphan_filters:
                context.gap_report = dict(context.gap_report or {})
                context.gap_report["semantic_router_quality_gate"] = {
                    "status": "failed",
                    "orphan_filters": orphan_filters,
                }
                return context
            context.semantic_route = "SEMANTIC_SQL"
            context.semantic_compiled_sql = compiled.sql
            context.semantic_plan = SemanticQueryPlan(
                metric=self._normalize_registry_member_name(result.measure),
                dimension=(
                    self._normalize_registry_member_name(result.dimensions[0])
                    if result.dimensions
                    else None
                ),
                time_semantics=self._normalize_registry_member_name(result.time_dimension),
                domain=context.domain,
                filters=[filter_.model_dump(mode="json") for filter_ in result.filters],
                requires_clarification=False,
                confidence=result.confidence,
            )
            context.retrieved_metadata = self._semantic_retrieval_context(context.semantic_plan, compiled)
            context.guardrail_contract = self._semantic_guardrail_contract({}, compiled)
            candidate = self._semantic_sql_candidate(compiled.sql, compiled)
            context.sql_candidates = [candidate]
            context.selected_sql = None
        except Exception:
            logger.exception("Semantic LLM router failed; falling back to existing pipeline.")
        return context

    def _semantic_quality_gate(self, sql: str, question: str, parameters: list[Any] | None = None) -> list[str]:
        question_text = question.lower()
        orphan_filters: list[str] = []
        filter_values = [*self._filter_literal_values(sql)]
        filter_values.extend(str(value) for value in parameters or [] if isinstance(value, str))
        for value in filter_values:
            normalized = value.lower()
            candidates = {
                normalized,
                normalized.replace("_", " "),
                normalized.replace("-", " "),
            }
            if not any(candidate and candidate in question_text for candidate in candidates):
                orphan_filters.append(value)
        return list(dict.fromkeys(orphan_filters))

    def _filter_literal_values(self, sql: str) -> list[str]:
        try:
            statement = sqlglot.parse_one(sql)
        except sqlglot.errors.ParseError:
            return []

        filter_roots: list[exp.Expression] = []
        filter_roots.extend(where.this for where in statement.find_all(exp.Where) if where.this is not None)
        for case in statement.find_all(exp.Case):
            for condition in case.args.get("ifs") or []:
                if condition.this is not None:
                    filter_roots.append(condition.this)

        values: list[str] = []
        for root in filter_roots:
            for literal in root.find_all(exp.Literal):
                if literal.this is None:
                    continue
                values.append(str(literal.this))
        return list(dict.fromkeys(values))

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
        if context.semantic_context:
            context.context_prompt = self._inject_semantic_context(context.context_prompt, context.semantic_context)
        if context.semantic_route == "SEMANTIC_ASSISTED_LLM" and context.guardrail_contract:
            context.context_prompt = self._inject_guardrail_contract(
                context.context_prompt,
                context.guardrail_contract,
            )
        if context.semantic_route in {"SEMANTIC_ASSISTED_LLM", "BASELINE_LLM"} and context.gap_report:
            context.context_prompt = self._inject_gap_report(context.context_prompt, context.gap_report)
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
        self._validate_candidates(context, allowed_tables, allowed_columns)
        if self._should_retry_without_guardrails(context):
            self._retry_without_guardrails(context, allowed_tables, allowed_columns)
        return context

    def _validate_candidates(
        self,
        context: PipelineContext,
        allowed_tables: set[str],
        allowed_columns: dict[str, set[str]],
    ) -> None:
        if context.semantic_plan is None:
            return
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

    def _should_retry_without_guardrails(self, context: PipelineContext) -> bool:
        if context.semantic_route != "SEMANTIC_ASSISTED_LLM" or not context.guardrail_contract:
            return False
        failed = [candidate for candidate in context.sql_candidates if not (candidate.parse_success and not candidate.validation_errors)]
        if len(failed) != len(context.sql_candidates):
            return False
        context.semantic_retry_count += len(failed)
        return context.semantic_retry_count >= 2

    def _retry_without_guardrails(
        self,
        context: PipelineContext,
        allowed_tables: set[str],
        allowed_columns: dict[str, set[str]],
    ) -> None:
        context.semantic_route = "BASELINE_LLM"
        context.guardrail_contract = None
        if context.semantic_plan is not None and context.retrieved_metadata is not None:
            context.context_prompt = self.context_builder.build(context.question, context.semantic_plan, context.retrieved_metadata)
        context.sql_candidates = self.candidate_generator.generate_candidates(context)
        self._validate_candidates(context, allowed_tables, allowed_columns)

    def repair(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("repair")
        if context.semantic_route == "SEMANTIC_SQL" and any(candidate.validation_errors for candidate in context.sql_candidates):
            self._fallback_from_semantic_sql(context, "The deterministic semantic SQL failed shared validation.")
            return context
        self.repair_loop.repair(context, self.sql_validator)
        return context

    def select(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("select")
        context.selected_sql = self.selector.select(context.sql_candidates)
        context.selection_log = list(self.selector.selection_log)
        return context

    def run_llm_judge(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("run_llm_judge")
        if context.selected_sql is None or not context.selected_sql.sql:
            return context

        while True:
            result = self._judge_selected_sql(context)
            if result is None:
                return context

            context.llm_judge_result = {
                "pass": result.pass_,
                "reasoning": result.reasoning,
                "confidence": result.confidence,
                "retry_count": context.llm_judge_retry_count,
            }
            if result.pass_:
                return context

            if context.semantic_route == "SEMANTIC_SQL":
                self._fallback_from_semantic_sql(context, result.reasoning)
                if context.error or context.selected_sql is None or not context.selected_sql.sql:
                    return context
                context.trace.append("run_llm_judge")
                continue

            if context.llm_judge_retry_count >= 3:
                self._accept_judge_failure_with_warning(context, result.reasoning)
                return context

            context.llm_judge_retry_count += 1
            self._retry_after_judge_failure(context, result.reasoning)
            if context.error or context.selected_sql is None or not context.selected_sql.sql:
                return context
            context.trace.append("run_llm_judge")

    def _judge_selected_sql(self, context: PipelineContext) -> Any | None:
        if context.selected_sql is None:
            return None
        try:
            judge_context = {
                "legacy_plan": context.semantic_plan.model_dump(mode="json") if context.semantic_plan else None,
                "semantic_route": context.semantic_route,
                "semantic_context": context.semantic_context,
                "semantic_result": context.semantic_result,
            }
            return self.llm_judge.judge(
                context.question,
                context.selected_sql.sql,
                context.semantic_route or context.selected_sql.generation_strategy,
                judge_context,
            )
        except Exception as exc:
            logger.warning("LLM judge unavailable; accepting selected SQL without semantic judge verdict: %s", exc)
            context.llm_judge_result = {
                "pass": True,
                "reasoning": f"LLM judge unavailable: {exc}",
                "confidence": 0.0,
                "retry_count": context.llm_judge_retry_count,
                "warning": True,
            }
            return None

    def _retry_after_judge_failure(self, context: PipelineContext, reasoning: str) -> None:
        feedback = (
            "The previous SQL attempt was rejected by an independent semantic judge. "
            f"Reason: {reasoning}. Review the metric, filters, dimensions, grouping, and time logic before trying again."
        )
        context.context_prompt = self._inject_llm_judge_feedback(context.context_prompt or "", feedback)
        context.selected_sql = None
        context.sql_candidates = []
        context = self.generate_candidates(context)
        context = self.validate(context)
        if context.error:
            return
        context = self.repair(context)
        if context.error:
            return
        context = self.select(context)

    def _accept_judge_failure_with_warning(self, context: PipelineContext, reasoning: str) -> None:
        if context.llm_judge_result is not None:
            context.llm_judge_result["warning"] = True
        if context.selected_sql is None:
            return
        warning = f"Judge warning: {reasoning}"
        if warning not in context.selected_sql.assumptions:
            context.selected_sql.assumptions.append(warning)
        if reasoning and f"[Judge: {reasoning}]" not in context.selected_sql.reasoning_summary:
            context.selected_sql.reasoning_summary += f" [Judge: {reasoning}]"

    def _fallback_from_semantic_sql(self, context: PipelineContext, reason: str) -> None:
        if context.semantic_plan is None or context.retrieved_metadata is None:
            context.error = f"Cannot fall back from semantic SQL: {reason}"
            return
        context.semantic_route = "SEMANTIC_ASSISTED_LLM"
        context.semantic_compiled_sql = None
        context.selected_sql = None
        context.sql_candidates = []
        context.context_prompt = None
        self.build_context(context)
        if context.error:
            return
        context.context_prompt = self._inject_llm_judge_feedback(
            context.context_prompt or "",
            f"The deterministic semantic SQL was rejected. {reason}",
        )
        self.generate_candidates(context)
        self.validate(context)
        if context.error:
            return
        self.repair_loop.repair(context, self.sql_validator)
        context.selected_sql = self.selector.select(context.sql_candidates)

    def explain(self, context: PipelineContext) -> PipelineContext:
        context.trace.append("explain")
        if context.semantic_route == "SEMANTIC_SQL" and context.selected_sql is not None and context.semantic_plan is None:
            context.explanation = self._semantic_sql_explanation(context.selected_sql)
            return context
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
            source_path = self._workspace_semantic_engine_root() / "src"
            if not source_path.exists():
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
        root = self._workspace_semantic_engine_root() / "semantic_models"
        if not root.exists():
            root = Path.home() / "semantic_modeling" / "semantic_models"
        commerce = root / "commerce"
        return commerce if commerce.exists() else root

    def _workspace_semantic_engine_root(self) -> Path:
        return Path(__file__).resolve().parents[4] / "semantic_modeling"

    def _load_semantic_snapshot(self, domain: str | None = None) -> Any | None:
        pipeline = self._semantic_pipeline(domain)
        snapshot = getattr(pipeline, "snapshot", None)
        if snapshot is not None:
            return snapshot
        if isinstance(pipeline, dict):
            return pipeline.get("snapshot")
        return None

    def _build_catalog_listing(self, snapshot: Any) -> dict[str, list[dict[str, Any]]]:
        listing: dict[str, list[dict[str, Any]]] = {
            "measures": [],
            "dimensions": [],
            "time_dimensions": [],
            "segments": [],
        }
        for entity in getattr(snapshot, "entities", {}).values():
            for measure in getattr(entity, "measures", []):
                listing["measures"].append(
                    {
                        "name": measure.name,
                        "entity": entity.name,
                        "aggregation": measure.aggregation,
                        "column": measure.expr,
                    }
                )
            for dimension in getattr(entity, "dimensions", []):
                listing["dimensions"].append(
                    {
                        "name": dimension.name,
                        "entity": entity.name,
                        "column": dimension.expr,
                    }
                )
            for dimension in getattr(entity, "time_dimensions", []):
                listing["time_dimensions"].append(
                    {
                        "name": dimension.name,
                        "entity": entity.name,
                        "column": dimension.expr,
                        "granularities": dimension.granularities,
                    }
                )
            for segment in getattr(entity, "segments", []):
                listing["segments"].append(
                    {
                        "name": segment.name,
                        "entity": entity.name,
                        "filters": [filter_.model_dump(mode="json") for filter_ in segment.filters],
                    }
                )
        return listing

    def _llm_router_generate(self, prompt: str) -> str:
        gateway = getattr(self.candidate_generator, "llm_gateway", None)
        system_prompt = (
            "Return ONLY the requested semantic-router JSON object. "
            "Do not generate SQL and do not include markdown."
        )
        if gateway is not None and hasattr(gateway, "generate_text"):
            return gateway.generate_text(prompt, system_prompt=system_prompt)
        provider = getattr(gateway, "provider", None)
        if provider is not None:
            raw = provider.generate("\n\n".join([system_prompt, prompt]))
            if isinstance(raw, str):
                return raw
            if hasattr(raw, "model_dump_json"):
                return raw.model_dump_json()
            return json.dumps(raw)
        if gateway is not None:
            response = gateway.generate(prompt)
            return response.model_dump_json()
        raise RuntimeError("No LLM gateway is available for semantic routing.")

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

    def _semantic_guardrail_contract(self, result: Any, compiled_query: Any) -> dict[str, Any] | None:
        contract = self._model_dump(self._result_value(result, "guardrail_contract"))
        if contract:
            return contract

        lineage = self._model_dump(self._result_value(compiled_query, "lineage")) or {}
        tables = list(lineage.get("tables") or [])
        selected_view = lineage.get("selected_view") or self._selected_view_from_result(result) or (tables[0] if tables else "semantic_sql")
        columns = self._lineage_columns(lineage)
        metric_names = list((lineage.get("measures") or {}).keys()) if isinstance(lineage.get("measures"), dict) else []
        metric_models = [metric for metric in self.registry_data.metrics if metric.metric in metric_names]
        if not metric_models:
            metric_models = [
                metric
                for metric in self.registry_data.metrics
                if metric.measure is not None and metric.measure.column in columns
            ]

        measures: list[dict[str, Any]] = []
        dimensions: list[dict[str, Any]] = []
        time_dimensions: list[dict[str, Any]] = []
        for metric in metric_models:
            if metric.measure:
                measures.append(
                    {
                        "name": metric.metric,
                        "entity": metric.measure.table,
                        "kind": "measure",
                        "column": metric.measure.column,
                        "aggregation": metric.aggregation,
                    }
                )
                if metric.measure.table not in tables:
                    tables.append(metric.measure.table)
            if metric.physical_time_column:
                time_dimensions.append(
                    {
                        "name": metric.default_time_dimension or metric.physical_time_column,
                        "entity": metric.measure.table if metric.measure else selected_view,
                        "kind": "time_dimension",
                        "column": metric.physical_time_column,
                    }
                )
            for dimension_name in metric.allowed_dimensions:
                dimension = self._dimension_by_name(dimension_name)
                mapping = self._dimension_mapping_for_tables(dimension, tables)
                if dimension and mapping:
                    dimensions.append(
                        {
                            "name": dimension.dimension,
                            "entity": mapping["table"],
                            "kind": "dimension",
                            "column": mapping["column"],
                        }
                    )

        if not tables and not measures and not columns:
            return None

        parsed_tables, parsed_columns = self._tables_and_columns_from_sql(self._result_value(compiled_query, "sql") or "")
        for table in parsed_tables:
            if table not in tables:
                tables.append(table)
        for column in parsed_columns:
            if column not in columns:
                columns.append(column)

        return {
            "selected_view": selected_view,
            "entities": [{"name": table, "table": table} for table in tables],
            "measures": measures or [{"name": column, "column": column, "kind": "measure"} for column in columns],
            "dimensions": list({json.dumps(item, sort_keys=True): item for item in dimensions}.values()),
            "time_dimensions": list({json.dumps(item, sort_keys=True): item for item in time_dimensions}.values()),
            "relationships": [],
            "invariants": ["Do not change tables, measures, or join structure from the governed semantic SQL seed."],
        }

    def _semantic_plan_from_engine_result(
        self,
        result: Any,
        compiled_query: Any,
        context: PipelineContext,
    ) -> SemanticQueryPlan:
        resolution = self._result_value(result, "resolution")
        metric = self._first_resolution_member(resolution, "measure")
        dimension = self._first_resolution_member(resolution, "dimension")
        time_semantics = self._first_resolution_member(resolution, "time_dimension")

        lineage = self._model_dump(self._result_value(compiled_query, "lineage")) or {}
        if metric is None:
            measures = lineage.get("measures")
            if isinstance(measures, dict) and measures:
                metric = self._normalize_registry_member_name(next(iter(measures.keys())))

        columns = self._lineage_columns(lineage)
        if metric is None:
            metric = self._metric_from_columns(columns)
        metric_model = self._metric_by_name(metric)
        metric = metric_model.metric if metric_model is not None else metric
        if time_semantics is None and metric_model is not None:
            time_semantics = metric_model.default_time_dimension
        else:
            time_semantics = self._normalize_registry_member_name(time_semantics)
        if dimension is None and metric_model is not None:
            dimension = self._dimension_from_question(context.question, metric_model)
        dimension_model = self._dimension_by_name(dimension)
        dimension = dimension_model.dimension if dimension_model is not None else dimension

        return SemanticQueryPlan(
            metric=metric,
            dimension=dimension,
            time_semantics=time_semantics,
            domain=context.domain,
            filters=[],
            requires_clarification=False,
            confidence=1.0,
        )

    def _semantic_retrieval_context(self, semantic_plan: SemanticQueryPlan | None, compiled_query: Any) -> RetrievalResult:
        result = RetrievalResult()
        lineage = self._model_dump(self._result_value(compiled_query, "lineage")) or {}
        table_names = list(lineage.get("tables") or [])
        metric = self._metric_by_name(semantic_plan.metric if semantic_plan else None)
        if metric and metric.measure and metric.measure.table not in table_names:
            table_names.insert(0, metric.measure.table)
        result.candidate_tables = [
            ScoredCandidate(name=table_name, score=1.0, description="Governed semantic SQL source.", domain=semantic_plan.domain or "")
            for table_name in table_names
        ]
        if semantic_plan and semantic_plan.metric:
            result.candidate_metrics = [
                ScoredCandidate(name=semantic_plan.metric, score=1.0, description="Governed semantic metric.", domain=semantic_plan.domain or "")
            ]
        return result

    def _first_resolution_member(self, resolution: Any, member_type: str) -> str | None:
        term_resolutions = self._result_value(resolution, "term_resolutions") or []
        for item in term_resolutions:
            if self._result_value(item, "member_type") == member_type:
                member = self._result_value(item, "matched_member")
                if member:
                    return self._normalize_registry_member_name(str(member))
        return None

    def _selected_view_from_result(self, result: Any) -> str | None:
        audit = self._result_value(result, "audit")
        selected_view = self._result_value(audit, "selected_view")
        if selected_view:
            return str(selected_view)
        resolution = self._result_value(result, "resolution")
        route_decision = self._result_value(resolution, "route_decision")
        selected_view = self._result_value(route_decision, "selected_view")
        return str(selected_view) if selected_view else None

    def _metric_by_name(self, metric_name: str | None) -> MetricYaml | None:
        if metric_name is None:
            return None
        normalized = self._normalize_registry_member_name(metric_name)
        for metric in self.registry_data.metrics:
            if metric.metric == normalized:
                return metric
        return None

    def _dimension_by_name(self, dimension_name: str | None) -> DimensionYaml | None:
        if dimension_name is None:
            return None
        normalized = self._normalize_registry_member_name(dimension_name)
        for dimension in self.registry_data.dimensions:
            if dimension.dimension == normalized:
                return dimension
        return None

    def _normalize_registry_member_name(self, value: str | None) -> str | None:
        if value is None:
            return None
        return value.rsplit(".", 1)[-1]

    def _metric_from_columns(self, columns: list[str]) -> str | None:
        for column in columns:
            for metric in self.registry_data.metrics:
                if metric.measure is not None and metric.measure.column == column:
                    return metric.metric
        return None

    def _dimension_from_question(self, question: str, metric: MetricYaml) -> str | None:
        question_text = question.lower()
        for dimension_name in metric.allowed_dimensions:
            dimension = self._dimension_by_name(dimension_name)
            if dimension is None:
                continue
            names = [dimension.dimension, *dimension.synonyms]
            if any(name.replace("_", " ").lower() in question_text for name in names):
                return dimension.dimension
        return None

    def _dimension_mapping_for_tables(self, dimension: DimensionYaml | None, tables: list[str]) -> dict[str, str] | None:
        if dimension is None:
            return None
        table_set = set(tables)
        for mapping in dimension.physical_mappings:
            if mapping.table in table_set:
                return {"table": mapping.table, "column": mapping.column}
        if dimension.physical_mappings:
            mapping = dimension.physical_mappings[0]
            return {"table": mapping.table, "column": mapping.column}
        return None

    def _tables_and_columns_from_sql(self, sql: str) -> tuple[list[str], list[str]]:
        if not sql:
            return [], []
        try:
            statement = sqlglot.parse_one(sql)
        except sqlglot.errors.ParseError:
            return [], []
        tables = [table.name for table in statement.find_all(exp.Table) if table.name]
        columns = [column.name for column in statement.find_all(exp.Column) if column.name]
        return list(dict.fromkeys(tables)), list(dict.fromkeys(columns))

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

    def _inject_guardrail_contract(self, prompt: str, contract: dict[str, Any], compiled_sql_seed: str | None = None) -> str:
        contract_text = json.dumps(contract, sort_keys=True)
        parts = [
            prompt,
            "[GuardrailContract]",
            "The SQL must use only the tables, columns, measures, dimensions, time dimensions, joins, segments, and invariants in this contract.",
            "<guardrail_contract>\n" + contract_text + "\n</guardrail_contract>",
        ]
        if compiled_sql_seed:
            parts.extend(
                [
                    "[Compiled SQL Seed]",
                    compiled_sql_seed,
                    (
                        "Your task: Review the compiled SQL above. If it correctly answers the question, return it as-is. "
                        "If improvements are needed (ORDER BY, LIMIT, formatting), refine it. "
                        "Do NOT change tables, measures, or join relationships."
                    ),
                ]
            )
        return "\n\n".join(parts)

    def _inject_semantic_context(self, prompt: str, semantic_context: dict[str, Any]) -> str:
        return "\n\n".join(
            [
                prompt,
                "[Semantic Context]",
                "Use these resolved business semantics when they help answer the question. "
                "Do not assume that missing members are modeled.",
                "<semantic_context>\n" + json.dumps(semantic_context, sort_keys=True) + "\n</semantic_context>",
            ]
        )

    def _inject_gap_report(self, prompt: str, gap_report: dict[str, Any]) -> str:
        missing_members = self._gap_items(
            gap_report,
            "missing_members",
            "missing_measures",
            "missing_dimensions",
        )
        gap_text = "\n".join(
            [
                "[Semantic Engine Gap Report]",
                "The governed semantic model could not fully resolve this question:",
                f"- Unresolved terms: {self._json_list(self._gap_items(gap_report, 'unresolved_terms'))}",
                f"- Missing members: {self._json_list(missing_members)}",
                f"- Suggested model additions: {self._json_list(self._gap_items(gap_report, 'suggested_model_additions'))}",
                "",
                "Consider whether existing governed members can satisfy the intent before generating raw SQL.",
            ]
        )
        return "\n\n".join([prompt, gap_text])

    def _inject_llm_judge_feedback(self, prompt: str, feedback: str) -> str:
        feedback_text = "\n".join(
            [
                "[Previous Attempt Feedback]",
                feedback,
            ]
        )
        return "\n\n".join(part for part in [prompt, feedback_text] if part)

    def _gap_items(self, gap_report: dict[str, Any], *keys: str) -> list[Any]:
        items: list[Any] = []
        for key in keys:
            value = gap_report.get(key) or []
            if isinstance(value, list):
                items.extend(value)
            else:
                items.append(value)
        deduped: list[Any] = []
        seen: set[str] = set()
        for item in items:
            marker = json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else str(item)
            if marker not in seen:
                seen.add(marker)
                deduped.append(item)
        return deduped

    def _json_list(self, values: list[Any]) -> str:
        return json.dumps(values, sort_keys=True)

    def _semantic_sql_explanation(self, sql_candidate: SQLCandidate) -> SQLExplanation:
        table = sql_candidate.tables_used[0] if sql_candidate.tables_used else ""
        return SQLExplanation(
            table_selected=table,
            table_reason="Selected by the governed semantic engine.",
            columns_used=list(sql_candidate.columns_used),
            assumptions=list(sql_candidate.assumptions),
            caveats=sql_candidate.validation_errors,
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
