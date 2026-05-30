from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.semantic_registry.metadata.models import TableMetadata
from src.semantic_registry.yaml_schema.schemas import MetricYaml as SemanticMetricYaml
from src.semantic_registry.yaml_schema.schemas import TermYaml as SemanticTermYaml


class RetrievalDocType(StrEnum):
    table = "table"
    term = "term"
    metric = "metric"
    dimension = "dimension"
    entity = "entity"


class RetrievalDoc(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    doc_type: RetrievalDocType
    doc_name: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def _line(label: str, value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value)
    return f"{label}: {value}"


def generate_table_doc(table: TableMetadata) -> str:
    lines = [
        _line("Table", table.table_name),
        _line("Domain", table.domain),
        _line("Description", table.description),
        _line("Certified", table.certified),
        _line("Grain", table.grain),
        _line("Partition column", table.partition_column),
        _line("Owner", table.owner),
    ]
    if table.columns:
        column_lines = [
            f"- {column.column_name} ({column.data_type}): {column.description}".strip()
            for column in table.columns
        ]
        lines.append("Columns:\n" + "\n".join(column_lines))
    if table.join_paths:
        join_lines = [
            f"- {join.from_table} -> {join.to_table}: {join.join_condition} "
            f"({join.relationship}, fanout risk {join.fanout_risk})"
            for join in table.join_paths
        ]
        lines.append("Join paths:\n" + "\n".join(join_lines))
    if table.caveats:
        lines.append("Known caveats:\n" + "\n".join(f"- {caveat}" for caveat in table.caveats))
    return "\n".join(line for line in lines if line)


def generate_all_table_docs(tables: list[TableMetadata]) -> list[tuple[str, str]]:
    return [(table.table_name, generate_table_doc(table)) for table in tables]


def generate_term_doc(term: SemanticTermYaml) -> str:
    lines = [
        _line("Term", term.term),
        _line("Domain", term.domain),
        _line("Description", term.description),
        _line("Synonyms", term.synonyms),
        _line("Candidate concepts", term.candidate_concepts),
        _line("Default concept by domain", term.default_concept_by_domain),
        _line("Ambiguity level", term.ambiguity_level),
    ]
    return "\n".join(line for line in lines if line)


def generate_metric_doc(metric: SemanticMetricYaml) -> str:
    mapping = None
    if metric.measure is not None:
        mapping = f"{metric.measure.table}.{metric.measure.column}"
    lines = [
        _line("Metric", metric.metric),
        _line("Concept", metric.concept),
        _line("Description", metric.description),
        _line("Type", metric.type),
        _line("Aggregation", metric.aggregation),
        _line("Unit", metric.unit),
        _line("Allowed dimensions", metric.allowed_dimensions),
        _line("Default time dimension", metric.default_time_dimension),
        _line("Physical time column", metric.physical_time_column),
        _line("Physical mapping", mapping),
        _line("Expression", metric.expression),
    ]
    return "\n".join(line for line in lines if line)


__all__ = [
    "RetrievalDoc",
    "RetrievalDocType",
    "SemanticMetricYaml",
    "SemanticTermYaml",
    "generate_all_table_docs",
    "generate_metric_doc",
    "generate_table_doc",
    "generate_term_doc",
]
