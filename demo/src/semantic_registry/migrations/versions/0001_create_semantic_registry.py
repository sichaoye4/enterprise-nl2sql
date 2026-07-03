"""create semantic registry tables

Revision ID: 0001_create_semantic_registry
Revises:
Create Date: 2026-05-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_create_semantic_registry"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "semantic"


def status_column() -> sa.Column:
    return sa.Column(
        "status",
        sa.Enum("draft", "reviewed", "certified", "deprecated", name="semanticstatus", native_enum=False),
        nullable=False,
        server_default="draft",
    )


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def id_column() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False)


def str_array() -> postgresql.ARRAY:
    return postgresql.ARRAY(sa.String())


def upgrade() -> None:
    op.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))

    op.create_table(
        "semantic_terms",
        id_column(),
        sa.Column("term", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("synonyms", str_array(), nullable=False),
        sa.Column("candidate_concepts", str_array(), nullable=False),
        sa.Column("default_concept_by_domain", sa.JSON(), nullable=False),
        sa.Column("ambiguity_level", sa.Enum("low", "medium", "high", name="ambiguitylevel", native_enum=False), nullable=False, server_default="low"),
        sa.Column("clarification_required_when", str_array(), nullable=False),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=100), nullable=False),
        status_column(),
        *timestamps(),
        sa.UniqueConstraint("term"),
        schema=SCHEMA,
    )

    op.create_table(
        "semantic_concepts",
        id_column(),
        sa.Column("concept", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=100), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("type", sa.String(length=100), nullable=False),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("related_but_different", sa.JSON(), nullable=False),
        sa.Column("canonical_metric", sa.String(length=255), nullable=True),
        status_column(),
        *timestamps(),
        sa.UniqueConstraint("concept"),
        schema=SCHEMA,
    )

    op.create_table(
        "semantic_metrics",
        id_column(),
        sa.Column("metric", sa.String(length=255), nullable=False),
        sa.Column("concept", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("type", sa.Enum("simple_sum", "simple_count", "count", "distinct_count", "ratio", "advanced", name="metrictype", native_enum=False), nullable=False, server_default="simple_sum"),
        sa.Column("measure", sa.JSON(), nullable=True),
        sa.Column("aggregation", sa.String(length=100), nullable=True),
        sa.Column("unit", sa.String(length=100), nullable=True),
        sa.Column("default_time_dimension", sa.String(length=255), nullable=True),
        sa.Column("physical_time_column", sa.String(length=255), nullable=True),
        sa.Column("allowed_dimensions", str_array(), nullable=False),
        sa.Column("numerator", sa.JSON(), nullable=True),
        sa.Column("denominator", sa.JSON(), nullable=True),
        sa.Column("expression", sa.Text(), nullable=True),
        sa.Column("owner", sa.String(length=255), nullable=False),
        status_column(),
        *timestamps(),
        sa.ForeignKeyConstraint(["concept"], [f"{SCHEMA}.semantic_concepts.concept"]),
        sa.UniqueConstraint("metric"),
        schema=SCHEMA,
    )

    op.create_table(
        "semantic_dimensions",
        id_column(),
        sa.Column("dimension", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("entity", sa.String(length=255), nullable=True),
        sa.Column("synonyms", str_array(), nullable=False),
        sa.Column("physical_mappings", sa.JSON(), nullable=False),
        status_column(),
        *timestamps(),
        sa.UniqueConstraint("dimension"),
        schema=SCHEMA,
    )

    op.create_table(
        "semantic_entities",
        id_column(),
        sa.Column("entity", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("primary_keys", str_array(), nullable=False),
        sa.Column("related_entities", str_array(), nullable=False),
        sa.Column("ambiguity_notes", sa.Text(), nullable=True),
        status_column(),
        *timestamps(),
        sa.UniqueConstraint("entity"),
        schema=SCHEMA,
    )

    op.create_table(
        "semantic_physical_mappings",
        id_column(),
        sa.Column("semantic_type", sa.Enum("term", "concept", "metric", "dimension", "entity", name="semantictype", native_enum=False), nullable=False, server_default="metric"),
        sa.Column("semantic_name", sa.String(length=255), nullable=False),
        sa.Column("physical_table", sa.String(length=255), nullable=False),
        sa.Column("physical_column", sa.String(length=255), nullable=False),
        sa.Column("transformation", sa.Text(), nullable=True),
        sa.Column("granularity", sa.String(length=255), nullable=True),
        status_column(),
        *timestamps(),
        sa.UniqueConstraint("semantic_type", "semantic_name", "physical_table", "physical_column", name="uq_semantic_physical_mapping"),
        schema=SCHEMA,
    )

    op.create_table(
        "semantic_join_paths",
        id_column(),
        sa.Column("join_path_name", sa.String(length=255), nullable=False),
        sa.Column("from_table", sa.String(length=255), nullable=False),
        sa.Column("to_table", sa.String(length=255), nullable=False),
        sa.Column("relationship", sa.Enum("one_to_one", "many_to_one", "one_to_many", "many_to_many", name="joinrelationship", native_enum=False), nullable=False, server_default="many_to_one"),
        sa.Column("join_condition", sa.Text(), nullable=False),
        sa.Column("safe_for_metrics", str_array(), nullable=False),
        sa.Column("fanout_risk", sa.Enum("low", "medium", "high", name="fanoutrisk", native_enum=False), nullable=False, server_default="low"),
        sa.Column("notes", sa.Text(), nullable=True),
        status_column(),
        *timestamps(),
        sa.UniqueConstraint("join_path_name"),
        schema=SCHEMA,
    )

    for table, columns in {
        "semantic_terms": ["term", "domain", "status"],
        "semantic_concepts": ["concept", "domain", "status"],
        "semantic_metrics": ["metric", "status"],
        "semantic_dimensions": ["dimension", "status"],
        "semantic_entities": ["entity", "status"],
        "semantic_join_paths": ["join_path_name", "status"],
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column], schema=SCHEMA)
    op.create_index("ix_semantic_join_paths_from_to", "semantic_join_paths", ["from_table", "to_table"], schema=SCHEMA)


def downgrade() -> None:
    for table in (
        "semantic_join_paths",
        "semantic_physical_mappings",
        "semantic_entities",
        "semantic_dimensions",
        "semantic_metrics",
        "semantic_concepts",
        "semantic_terms",
    ):
        op.drop_table(table, schema=SCHEMA)
    op.execute(sa.text(f"DROP SCHEMA IF EXISTS {SCHEMA}"))
