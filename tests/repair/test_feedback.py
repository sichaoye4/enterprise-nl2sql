from __future__ import annotations

from src.semantic_registry.repair import FeedbackCapture


def test_feedback_capture_stores_correct_data() -> None:
    capture = FeedbackCapture()

    record = capture.capture(
        query_id="query-1",
        original_sql="SELECT amount FROM orders",
        corrected_sql="SELECT SUM(amount) AS amount FROM orders",
        feedback_type="correct",
        user="analyst",
        comment="aggregate this metric",
    )

    assert capture.records == [record]
    assert record.query_id == "query-1"
    assert record.corrected_sql == "SELECT SUM(amount) AS amount FROM orders"
    assert record.user == "analyst"
