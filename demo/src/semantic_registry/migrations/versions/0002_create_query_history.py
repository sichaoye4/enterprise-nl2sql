"""create query history and feedback tables

Revision ID: 0002_create_query_history
Revises: 0001_create_semantic_registry
Create Date: 2026-05-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_create_query_history"
down_revision = "0001_create_semantic_registry"
branch_labels = None
depends_on = None

SCHEMA = "semantic"


def id_column() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False)


def upgrade() -> None:
    op.create_table(
        "query_logs",
        id_column(),
        sa.Column("query_id", sa.String(length=100), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(length=100), nullable=True),
        sa.Column("generated_sql", sa.Text(), nullable=False),
        sa.Column("semantic_plan_json", sa.JSON(), nullable=False),
        sa.Column("validation_results_json", sa.JSON(), nullable=False),
        sa.Column("execution_results_json", sa.JSON(), nullable=True),
        sa.Column("feedback_type", sa.String(length=100), nullable=True),
        sa.Column("corrected_sql", sa.Text(), nullable=True),
        sa.Column("user_comment", sa.Text(), nullable=True),
        sa.Column("reviewer", sa.String(length=255), nullable=True),
        sa.Column("metadata_snapshot_version", sa.String(length=100), nullable=True),
        sa.Column("model_version", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("user", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("query_id", name="uq_query_logs_query_id"),
        schema=SCHEMA,
    )
    op.create_index("ix_query_logs_query_id", "query_logs", ["query_id"], schema=SCHEMA)
    op.create_index("ix_query_logs_user", "query_logs", ["user"], schema=SCHEMA)
    op.create_index("ix_query_logs_domain", "query_logs", ["domain"], schema=SCHEMA)
    op.create_index("ix_query_logs_status", "query_logs", ["status"], schema=SCHEMA)

    op.create_table(
        "nl2sql_feedback",
        id_column(),
        sa.Column("query_id", sa.String(length=100), nullable=False),
        sa.Column("original_sql", sa.Text(), nullable=False),
        sa.Column("corrected_sql", sa.Text(), nullable=False),
        sa.Column("user", sa.String(length=255), nullable=False),
        sa.Column("feedback_type", sa.String(length=100), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_nl2sql_feedback_query_id", "nl2sql_feedback", ["query_id"], schema=SCHEMA)
    op.create_index("ix_nl2sql_feedback_user", "nl2sql_feedback", ["user"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("nl2sql_feedback", schema=SCHEMA)
    op.drop_table("query_logs", schema=SCHEMA)
