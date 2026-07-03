from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Enum, ForeignKeyConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.semantic_registry.models.base import Base, TimestampVersionMixin


SCHEMA_NAME = "semantic"


def string_array() -> ARRAY:
    return ARRAY(String).with_variant(JSON, "sqlite")  # type: ignore[return-value]


class SemanticStatus(StrEnum):
    draft = "draft"
    reviewed = "reviewed"
    certified = "certified"
    deprecated = "deprecated"


class AmbiguityLevel(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class MetricType(StrEnum):
    simple_sum = "simple_sum"
    simple_count = "simple_count"
    count = "count"
    distinct_count = "distinct_count"
    ratio = "ratio"
    advanced = "advanced"


class SemanticType(StrEnum):
    term = "term"
    concept = "concept"
    metric = "metric"
    dimension = "dimension"
    entity = "entity"


class JoinRelationship(StrEnum):
    one_to_one = "one_to_one"
    many_to_one = "many_to_one"
    one_to_many = "one_to_many"
    many_to_many = "many_to_many"


class FanoutRisk(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


def enum_column(enum_type: type[StrEnum], *, default: StrEnum | None = None) -> Mapped[Any]:
    return mapped_column(
        Enum(enum_type, native_enum=False, validate_strings=True),
        nullable=False,
        default=default,
        server_default=default.value if default is not None else None,
    )


class SemanticTerm(TimestampVersionMixin, Base):
    __tablename__ = "semantic_terms"
    __table_args__ = (
        Index("ix_semantic_terms_domain", "domain"),
        Index("ix_semantic_terms_status", "status"),
        {"schema": SCHEMA_NAME},
    )

    term: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    synonyms: Mapped[list[str]] = mapped_column(string_array(), nullable=False, default=list)
    candidate_concepts: Mapped[list[str]] = mapped_column(string_array(), nullable=False, default=list)
    default_concept_by_domain: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    ambiguity_level: Mapped[AmbiguityLevel] = enum_column(AmbiguityLevel, default=AmbiguityLevel.low)
    clarification_required_when: Mapped[list[str]] = mapped_column(string_array(), nullable=False, default=list)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[SemanticStatus] = enum_column(SemanticStatus, default=SemanticStatus.draft)


class SemanticConcept(TimestampVersionMixin, Base):
    __tablename__ = "semantic_concepts"
    __table_args__ = (
        Index("ix_semantic_concepts_domain", "domain"),
        Index("ix_semantic_concepts_status", "status"),
        {"schema": SCHEMA_NAME},
    )

    concept: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(100), nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String(100), nullable=False, default="metric_concept")
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    related_but_different: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    canonical_metric: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[SemanticStatus] = enum_column(SemanticStatus, default=SemanticStatus.draft)


class SemanticMetric(TimestampVersionMixin, Base):
    __tablename__ = "semantic_metrics"
    __table_args__ = (
        ForeignKeyConstraint(
            ["concept"],
            [f"{SCHEMA_NAME}.semantic_concepts.concept"],
            name="fk_semantic_metrics_concept",
            use_alter=True,
        ),
        Index("ix_semantic_metrics_status", "status"),
        {"schema": SCHEMA_NAME},
    )

    metric: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    concept: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[MetricType] = enum_column(MetricType, default=MetricType.simple_sum)
    measure: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    aggregation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(100), nullable=True)
    default_time_dimension: Mapped[str | None] = mapped_column(String(255), nullable=True)
    physical_time_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    allowed_dimensions: Mapped[list[str]] = mapped_column(string_array(), nullable=False, default=list)
    numerator: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    denominator: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    expression: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[SemanticStatus] = enum_column(SemanticStatus, default=SemanticStatus.draft)


class SemanticDimension(TimestampVersionMixin, Base):
    __tablename__ = "semantic_dimensions"
    __table_args__ = (
        Index("ix_semantic_dimensions_status", "status"),
        {"schema": SCHEMA_NAME},
    )

    dimension: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str | None] = mapped_column(String(255), nullable=True)
    synonyms: Mapped[list[str]] = mapped_column(string_array(), nullable=False, default=list)
    physical_mappings: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[SemanticStatus] = enum_column(SemanticStatus, default=SemanticStatus.draft)


class SemanticEntity(TimestampVersionMixin, Base):
    __tablename__ = "semantic_entities"
    __table_args__ = (
        Index("ix_semantic_entities_status", "status"),
        {"schema": SCHEMA_NAME},
    )

    entity: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    primary_keys: Mapped[list[str]] = mapped_column(string_array(), nullable=False, default=list)
    related_entities: Mapped[list[str]] = mapped_column(string_array(), nullable=False, default=list)
    ambiguity_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[SemanticStatus] = enum_column(SemanticStatus, default=SemanticStatus.draft)


class SemanticPhysicalMapping(TimestampVersionMixin, Base):
    __tablename__ = "semantic_physical_mappings"
    __table_args__ = (
        Index("ix_semantic_physical_mappings_semantic", "semantic_type", "semantic_name"),
        Index("ix_semantic_physical_mappings_status", "status"),
        UniqueConstraint(
            "semantic_type",
            "semantic_name",
            "physical_table",
            "physical_column",
            name="uq_semantic_physical_mapping",
        ),
        {"schema": SCHEMA_NAME},
    )

    semantic_type: Mapped[SemanticType] = enum_column(SemanticType, default=SemanticType.metric)
    semantic_name: Mapped[str] = mapped_column(String(255), nullable=False)
    physical_table: Mapped[str] = mapped_column(String(255), nullable=False)
    physical_column: Mapped[str] = mapped_column(String(255), nullable=False)
    transformation: Mapped[str | None] = mapped_column(Text, nullable=True)
    granularity: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[SemanticStatus] = enum_column(SemanticStatus, default=SemanticStatus.draft)


class SemanticJoinPath(TimestampVersionMixin, Base):
    __tablename__ = "semantic_join_paths"
    __table_args__ = (
        Index("ix_semantic_join_paths_from_to", "from_table", "to_table"),
        Index("ix_semantic_join_paths_status", "status"),
        {"schema": SCHEMA_NAME},
    )

    join_path_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    from_table: Mapped[str] = mapped_column(String(255), nullable=False)
    to_table: Mapped[str] = mapped_column(String(255), nullable=False)
    relationship: Mapped[JoinRelationship] = enum_column(JoinRelationship, default=JoinRelationship.many_to_one)
    join_condition: Mapped[str] = mapped_column(Text, nullable=False)
    safe_for_metrics: Mapped[list[str]] = mapped_column(string_array(), nullable=False, default=list)
    fanout_risk: Mapped[FanoutRisk] = enum_column(FanoutRisk, default=FanoutRisk.low)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[SemanticStatus] = enum_column(SemanticStatus, default=SemanticStatus.draft)
