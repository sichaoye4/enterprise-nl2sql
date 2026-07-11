from __future__ import annotations

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
        sections = [
            "You are generating SQL from a governed semantic registry context. Use SQLite syntax.",
            self._ddl_section(tables),
            self._domain_knowledge_section(retrieved_metadata.known_caveats),
            self._semantic_plan_section(semantic_plan),
            self._metrics_section(semantic_plan, retrieved_metadata),
            self._join_paths_section(tables),
            self._question_section(safe_question),
            self._rules_section(),
        ]
        return "\n\n".join(section for section in sections if section)

    def _question_section(self, question: str) -> str:
        return f"Original question:\n{question}"

    def _semantic_plan_section(self, semantic_plan: SemanticQueryPlan) -> str:
        metric = self._business_name(semantic_plan.metric)
        dimension = self._business_name(semantic_plan.dimension)
        time_semantics = self._business_name(semantic_plan.time_semantics)
        plan_fields = [
            ("Metric", metric),
            ("Dimension", dimension),
            ("Time range", semantic_plan.time_range),
            ("Time semantics", time_semantics),
            ("Domain", semantic_plan.domain),
        ]
        lines = ["Resolved semantic plan:"]
        lines.extend(f"- {label}: {value}" for label, value in plan_fields if value not in (None, "", [], {}))
        if semantic_plan.filters:
            lines.append(f"- Filters: {semantic_plan.filters}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    def _ddl_section(self, tables: list[TableMetadata]) -> str:
        if not tables:
            return "DDL Schema:\n- None"
        statements = [self._create_table_ddl(table) for table in tables]
        return "DDL Schema:\n```sql\n" + "\n\n".join(statements) + "\n```"

    def _create_table_ddl(self, table: TableMetadata) -> str:
        lines = [f"CREATE TABLE {self._quote_identifier(table.table_name)} ("]
        visible_columns = [column for column in table.columns if not column.is_pii]
        column_lines = [f"  {self._column_definition(column)}" for column in visible_columns]
        column_lines.extend(f"  {constraint}" for constraint in self._foreign_key_constraints(table))
        if not column_lines:
            column_lines.append("  -- No non-PII columns available in metadata")
        lines.append(",\n".join(column_lines))
        lines.append(");")
        return "\n".join(lines)

    def _column_definition(self, column: ColumnMetadata) -> str:
        parts = [self._quote_identifier(column.column_name), self._sqlite_type(column.data_type)]
        if not column.nullable:
            parts.append("NOT NULL")
        if column.default_value not in (None, ""):
            parts.append(f"DEFAULT {column.default_value}")
        return " ".join(parts)

    def _foreign_key_constraints(self, table: TableMetadata) -> list[str]:
        constraints: list[str] = []
        for join in table.join_paths:
            parsed = self._parse_simple_join_condition(join.join_condition, join.from_table, join.to_table)
            if parsed is None:
                continue
            from_column, to_column = parsed
            constraints.append(
                f"FOREIGN KEY ({self._quote_identifier(from_column)}) "
                f"REFERENCES {self._quote_identifier(join.to_table)} ({self._quote_identifier(to_column)})"
            )
        return constraints

    def _parse_simple_join_condition(self, condition: str, from_table: str, to_table: str) -> tuple[str, str] | None:
        identifier = r"`?([A-Za-z_][A-Za-z0-9_ .]*)`?"
        match = re.fullmatch(rf"\s*{identifier}\.{identifier}\s*=\s*{identifier}\.{identifier}\s*", condition)
        if not match:
            return None
        left_table, left_column, right_table, right_column = [part.strip("` ") for part in match.groups()]
        if left_table == from_table and right_table == to_table:
            return left_column, right_column
        if left_table == to_table and right_table == from_table:
            return right_column, left_column
        return None

    def _sqlite_type(self, data_type: str | None) -> str:
        normalized = (data_type or "").strip().lower()
        if not normalized:
            return "TEXT"
        if any(token in normalized for token in ("int", "serial", "bool")):
            return "INTEGER"
        if any(token in normalized for token in ("real", "double", "float", "numeric", "decimal", "number")):
            return "REAL"
        if any(token in normalized for token in ("date", "time")):
            return "TEXT"
        if "blob" in normalized or "binary" in normalized:
            return "BLOB"
        return "TEXT"

    def _quote_identifier(self, identifier: str) -> str:
        escaped_parts = [part.replace("`", "``") for part in identifier.split(".")]
        return ".".join(f"`{part}`" for part in escaped_parts if part)

    def _domain_knowledge_section(self, caveats: list[str]) -> str:
        if not caveats:
            return ""
        lines = ["Domain Knowledge / Hint:"]
        lines.extend(f"- {self._redact_sensitive_values(caveat)}" for caveat in caveats)
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
            if metric.physical_time_column:
                lines.append(f"  Time column: {metric.physical_time_column}")
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
            lines.append(f"- {join['from_table']} -> {join['to_table']}: {join['join_condition']}")
        return "\n".join(lines)

    def _join_paths(self, tables: list[TableMetadata]) -> list[dict[str, Any]]:
        return [
            {
                "from_table": join.from_table,
                "to_table": join.to_table,
                "join_condition": join.join_condition,
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
                "- Use SQLite dialect.",
                "- Use backtick quoting for table or column names that contain spaces or special characters.",
                "- Generate exactly one SELECT statement.",
                "- Do not use SELECT *.",
                "- Use only tables, columns, metric mappings, and join paths listed above.",
                "- When a filtered column name appears in the question or semantic plan, add the matching WHERE predicate.",
                "- For time-based questions, add a WHERE time-range filter using the resolved time column.",
                "- Prefer certified metric definitions and return business assumptions separately from the SQL.",
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
