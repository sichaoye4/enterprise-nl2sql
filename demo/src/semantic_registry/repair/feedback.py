from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel


class FeedbackRecord(BaseModel):
    query_id: str
    original_sql: str
    corrected_sql: str
    user: str
    feedback_type: str
    comment: str | None = None
    created_at: datetime


class FeedbackCapture:
    def __init__(self) -> None:
        self.records: list[FeedbackRecord] = []

    def capture(
        self,
        query_id: str,
        original_sql: str,
        corrected_sql: str,
        feedback_type: str,
        user: str,
        comment: str | None = None,
    ) -> FeedbackRecord:
        record = FeedbackRecord(
            query_id=query_id,
            original_sql=original_sql,
            corrected_sql=corrected_sql,
            user=user,
            feedback_type=feedback_type,
            comment=comment,
            created_at=datetime.now(timezone.utc),
        )
        self.records.append(record)
        return record


__all__ = ["FeedbackCapture", "FeedbackRecord"]
