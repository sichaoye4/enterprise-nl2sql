from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from src.semantic_registry.models.base import Base, GUID
from src.semantic_registry.models.entities import SCHEMA_NAME


class QueryLog(Base):
    __tablename__ = "query_logs"
    __table_args__ = (
        Index("ix_query_logs_query_id", "query_id"),
        Index("ix_query_logs_user", "user"),
        Index("ix_query_logs_domain", "domain"),
        Index("ix_query_logs_status", "status"),
        UniqueConstraint("query_id", name="uq_query_logs_query_id"),
        {"schema": SCHEMA_NAME},
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    query_id: Mapped[str] = mapped_column(String(100), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(String(100), nullable=True)
    generated_sql: Mapped[str] = mapped_column(Text, nullable=False, default="")
    semantic_plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    validation_results_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    execution_results_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    feedback_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    corrected_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_snapshot_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    user: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class FeedbackLog(Base):
    __tablename__ = "nl2sql_feedback"
    __table_args__ = (
        Index("ix_nl2sql_feedback_query_id", "query_id"),
        Index("ix_nl2sql_feedback_user", "user"),
        {"schema": SCHEMA_NAME},
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    query_id: Mapped[str] = mapped_column(String(100), nullable=False)
    original_sql: Mapped[str] = mapped_column(Text, nullable=False)
    corrected_sql: Mapped[str] = mapped_column(Text, nullable=False)
    user: Mapped[str] = mapped_column(String(255), nullable=False)
    feedback_type: Mapped[str] = mapped_column(String(100), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


__all__ = ["FeedbackLog", "QueryLog"]
