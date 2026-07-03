from __future__ import annotations

import json
import re
from typing import Any

from src.semantic_registry.metadata.models import ColumnMetadata, TableMetadata
from src.semantic_registry.metadata.provider import MetadataProvider
from src.semantic_registry.resolver.plan import SemanticQueryPlan
from src.semantic_registry.resolver.registry import SemanticRegistryData
from src.semantic_registry.retrieval.hybrid import RetrievalResult
from src.semantic_registry.yaml_schema.schemas import DimensionYaml, MetricYaml


class ContextBuilder:
    def __init__(
        self,
        registry_data: SemanticRegistryData | None = None,
        metadata_provider: MetadataProvider | None = None,
    ) -> None:
        self.registry_data = registry_data or SemanticRegistryData()
        self.metadata_provider = metadata_provider
        self.metrics_by_name = {metric.metric: metric for metric in self.registry_data.metrics}
        self.dimensions_by_name = {dimension.dimension: dimension for dimension in self.registry_data.dimensions}

    def build(
        self,
        question: str,
        semantic_plan: SemanticQueryPlan,
        retrieved_metadata: RetrievalResult,
    ) -> str:
        safe_question = self._redact_sensitive_values(question)
        tables = self._candidate_tables(semantic_plan, retrieved_metadata)
        physical_mapping = self._physical_mapping(semantic_plan)
        join_paths = self._join_paths(tables)
        context_json = {
            "question": safe_question,
            "semantic_plan": {
                "metric": semantic_plan.metric,
                "dimension": semantic_plan.dimension,
                "time_range": semantic_plan.time_range,
                "time_semantics": semantic_plan.time_semantics,
                "domain": semantic_plan.domain,
                "filters": semantic_plan.filters,
            },
            "physical_mapping": physical_mapping,
            "candidate_tables": [table.table_name for table in tables],
            "join_paths": join_paths,
            "known_caveats": retrieved_metadata.known_caveats,
        }
        sections = [
            "You are generating SQL from a governed semantic registry context.",
            self._question_section(safe_question),
            self._semantic_plan_section(semantic_plan),
            self._tables_section(tables, retrieved_metadata),
            self._metrics_section(semantic_plan, retrieved_metadata),
            self._join_paths_section(tables),
            self._caveats_section(retrieved_metadata.known_caveats),
            self._rules_section(),
            self._output_contract_section(),
            "<generation_context>\n" + json.dumps(context_json, sort_keys=True) + "\n</generation_context>",
        ]
        return "\n\n".join(section for section in sections if section)

    def _question_section(self, question: str) -> str:
        return f"Original question:\n{question}"

    def _semantic_plan_section(self, semantic_plan: SemanticQueryPlan) -> str:
        metric = self._business_name(semantic_plan.metric)
        dimension = self._business_name(semantic_plan.dimension)
        time_semantics = self._business_name(semantic_plan.time_semantics)
        lines = [
            "Resolved semantic plan:",
            f"- Metric: {metric or 'None'}",
            f"- Dimension: {dimension or 'None'}",
            f"- Time range: {semantic_plan.time_range or 'None'}",
            f"- Time semantics: {time_semantics or 'None'}",
            f"- Domain: {semantic_plan.domain or 'None'}",
        ]
        return "\n".join(lines)

    def _tables_section(self, tables: list[TableMetadata], retrieved_metadata: RetrievalResult) -> str:
        if not tables and not retrieved_metadata.candidate_tables:
            return "Candidate tables:\n- None"
        lines = ["Candidate tables:"]
        rendered = set()
        for table in tables:
            rendered.add(table.table_name)
            lines.append(f"- {table.table_name}: {self._redact_sensitive_values(table.description)}")
            if table.grain:
                lines.append(f"  Grain: {', '.join(table.grain)}")
            if table.partition_column:
                lines.append(f"  Partition column: {table.partition_column}")
            visible_columns = [column for column in table.columns if not column.is_pii]
            if visible_columns:
                lines.append("  Columns:")
                for column in visible_columns:
                    description = self._redact_sensitive_values(column.description)
                    lines.append(f"  - {column.column_name} ({column.data_type or 'unknown'}): {description}")
        for candidate in retrieved_metadata.candidate_tables:
            if candidate.name not in rendered:
                lines.append(f"- {candidate.name}: {self._redact_sensitive_values(candidate.description)}")
        if retrieved_metadata.candidate_columns:
            lines.append("Candidate columns:")
            for column in retrieved_metadata.candidate_columns:
                lines.append(f"- {column}")
        return "\n".join(lines)

    def _metrics_section(self, semantic_plan: SemanticQueryPlan, retrieved_metadata: RetrievalResult) -> str:
        metric_names = [semantic_plan.metric] if semantic_plan.metric else []
        metric_names.extend(candidate.name for candidate in retrieved_metadata.candidate_metrics)
        seen: set[str] = set()
        lines = ["Candidate metrics and physical mappings:"]
        for metric_name in metric_names:
            if not metric_name or metric_name in seen:
                continue
            seen.add(metric_name)
            metric = self.metrics_by_name.get(metric_name)
            if metric is None:
                lines.append(f"- {self._business_name(metric_name)}")
                continue
            lines.append(f"- {self._business_name(metric.metric)}: {metric.description}")
            if metric.measure:
                lines.append(f"  Physical mapping: {metric.measure.table}.{metric.measure.column}")
            if metric.expression:
                lines.append(f"  Expression: {metric.expression}")
            if metric.aggregation:
                lines.append(f"  Aggregation: {metric.aggregation}")
        return "\n".join(lines)

    def _join_paths_section(self, tables: list[TableMetadata]) -> str:
        lines = ["Join paths between candidate tables:"]
        joins = self._join_paths(tables)
        if not joins:
            lines.append("- None required or none available")
            return "\n".join(lines)
        for join in joins:
            lines.append(
                f"- {join['from_table']} -> {join['to_table']}: {join['join_condition']} "
                f"({join['relationship']}, fanout risk {join['fanout_risk']})"
            )
        return "\n".join(lines)

    def _join_paths(self, tables: list[TableMetadata]) -> list[dict[str, Any]]:
        return [
            {
                "from_table": join.from_table,
                "to_table": join.to_table,
                "relationship": str(join.relationship),
                "join_condition": join.join_condition,
                "safe_for_metrics": join.safe_for_metrics,
                "fanout_risk": str(join.fanout_risk),
            }
            for table in tables
            for join in table.join_paths
        ]

    def _caveats_section(self, caveats: list[str]) -> str:
        if not caveats:
            return "Known caveats:\n- None"
        lines = ["Known caveats:"]
        lines.extend(f"- {self._redact_sensitive_values(caveat)}" for caveat in caveats)
        return "\n".join(lines)

    def _rules_section(self) -> str:
        return "\n".join(
            [
                "Generation rules:",
                "- Generate exactly one SELECT statement.",
                "- Do not generate INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, or other write statements.",
                "- Do not use SELECT *.",
                "- Do not invent tables, columns, metrics, filters, or joins.",
                "- Use only the candidate tables, columns, metric mappings, and join paths listed above.",
                "- Prefer certified metric definitions and the resolved semantic plan.",
                "- Return business assumptions separately from the SQL.",
            ]
        )

    def _output_contract_section(self) -> str:
        return "\n".join(
            [
                "Output JSON format:",
                '{"sql": "...", "assumptions": [], "tables_used": [], "columns_used": [], '
                '"confidence": "high|medium|low", "reasoning_summary": "..."}',
            ]
        )

    def _candidate_tables(
        self,
        semantic_plan: SemanticQueryPlan,
        retrieved_metadata: RetrievalResult,
    ) -> list[TableMetadata]:
        tables: list[TableMetadata] = []
        seen: set[str] = set()
        for candidate in retrieved_metadata.candidate_tables:
            table = self.metadata_provider.get_table(candidate.name) if self.metadata_provider else None
            if table is None:
                table = TableMetadata(table_name=candidate.name, description=candidate.description, domain=candidate.domain or None)
            if table.table_name not in seen:
                seen.add(table.table_name)
                tables.append(table)

        derived = self._table_from_semantic_plan(semantic_plan)
        if derived and derived.table_name not in seen:
            tables.insert(0, derived)
        return tables

    def _table_from_semantic_plan(self, semantic_plan: SemanticQueryPlan) -> TableMetadata | None:
        metric = self.metrics_by_name.get(semantic_plan.metric or "")
        if metric is None or metric.measure is None:
            return None
        columns = [
            ColumnMetadata(
                column_name=metric.measure.column,
                data_type="numeric",
                description=f"Measure for {self._business_name(metric.metric)}.",
                concept=metric.metric,
                aggregation=metric.aggregation,
                unit=metric.unit,
            )
        ]
        if metric.physical_time_column:
            columns.append(
                ColumnMetadata(
                    column_name=metric.physical_time_column,
                    data_type="date",
                    description=f"Time column for {self._business_name(metric.default_time_dimension)}.",
                    concept=metric.default_time_dimension,
                )
            )
        dimension = self.dimensions_by_name.get(semantic_plan.dimension or "")
        if dimension:
            mapping = self._dimension_mapping_for_table(dimension, metric.measure.table)
            if mapping:
                columns.append(
                    ColumnMetadata(
                        column_name=mapping["column"],
                        data_type="text",
                        description=dimension.description,
                        concept=dimension.dimension,
                    )
                )
        return TableMetadata(
            table_name=metric.measure.table,
            description=f"Certified semantic source for {self._business_name(metric.metric)}.",
            certified=str(metric.status) == "certified",
            eligible_for_nl2sql=True,
            grain=[column.column_name for column in columns if column.concept != metric.metric],
            partition_column=metric.physical_time_column,
            columns=columns,
        )

    def _physical_mapping(self, semantic_plan: SemanticQueryPlan) -> dict[str, Any]:
        metric = self.metrics_by_name.get(semantic_plan.metric or "")
        if metric is None:
            return {}
        mapping: dict[str, Any] = {
            "aggregation": metric.aggregation,
            "metric_expression": metric.expression,
            "time_column": metric.physical_time_column,
        }
        if metric.measure:
            mapping["table"] = metric.measure.table
            mapping["metric_column"] = metric.measure.column
            mapping["metric_expression"] = metric.measure.column
        dimension = self.dimensions_by_name.get(semantic_plan.dimension or "")
        if dimension:
            dimension_mapping = self._dimension_mapping_for_table(dimension, mapping.get("table"))
            if dimension_mapping:
                mapping["dimension_table"] = dimension_mapping["table"]
                mapping["dimension_column"] = dimension_mapping["column"]
        return {key: value for key, value in mapping.items() if value not in (None, "", [], {})}

    def _dimension_mapping_for_table(self, dimension: DimensionYaml, table_name: str | None) -> dict[str, str] | None:
        if not dimension.physical_mappings:
            return None
        for mapping in dimension.physical_mappings:
            if table_name is not None and mapping.table == table_name:
                return {"table": mapping.table, "column": mapping.column}
        first = dimension.physical_mappings[0]
        return {"table": first.table, "column": first.column}

    def _business_name(self, value: str | None) -> str | None:
        if not value:
            return None
        concept_names = {concept.concept: concept.display_name for concept in self.registry_data.concepts}
        if value in concept_names:
            return concept_names[value]
        words = []
        for part in value.split("_"):
            words.append(part.upper() if part.lower() in {"gmv", "pii", "id"} else part.capitalize())
        return " ".join(words)

    def _redact_sensitive_values(self, text: str | None) -> str:
        if not text:
            return ""
        redacted = re.sub(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", text)
        redacted = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED_SSN]", redacted)
        redacted = re.sub(r"\b(?:\d[ -]*?){13,16}\b", "[REDACTED_CARD]", redacted)
        redacted = re.sub(r"\b(?:\+?\d[\d .()-]{7,}\d)\b", "[REDACTED_PHONE]", redacted)
        redacted = re.sub(r"(?i)(password\s*(?:is|=|:)\s*)\S+", r"\1[REDACTED_PASSWORD]", redacted)
        return redacted
