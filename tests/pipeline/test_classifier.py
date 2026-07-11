from __future__ import annotations

import pytest

from src.semantic_registry.pipeline.classifier import QuestionClassifier


@pytest.fixture
def classifier() -> QuestionClassifier:
    return QuestionClassifier()


@pytest.mark.parametrize("question", ["insert a row", "delete old orders", "drop orders", "create table sales"])
def test_write_intent_detected(classifier: QuestionClassifier, question: str) -> None:
    classification = classifier.classify(question)

    assert classification.write_intent is True


def test_write_intent_false_for_read_question(classifier: QuestionClassifier) -> None:
    classification = classifier.classify("show paid GMV by channel")

    assert classification.write_intent is False


@pytest.mark.parametrize("question", ["show user email", "list phone number", "find ssn values"])
def test_sensitive_data_intent_detected(classifier: QuestionClassifier, question: str) -> None:
    classification = classifier.classify(question)

    assert classification.sensitive_data_intent is True


def test_metric_by_dimension_query_type(classifier: QuestionClassifier) -> None:
    classification = classifier.classify("show revenue by channel")

    assert classification.query_type == "metric_by_dimension"


def test_comparison_query_type(classifier: QuestionClassifier) -> None:
    classification = classifier.classify("compare revenue vs gmv")

    assert classification.query_type == "comparison"


def test_top_n_query_type(classifier: QuestionClassifier) -> None:
    classification = classifier.classify("top 10 products by revenue")

    assert classification.query_type == "top_N"


@pytest.mark.parametrize("question", ["show monthly revenue", "daily paid GMV", "show GMV trend"])
def test_time_series_query_type(classifier: QuestionClassifier, question: str) -> None:
    classification = classifier.classify(question)

    assert classification.query_type == "time_series"


@pytest.mark.parametrize("question", ["show revenue last month", "show paid GMV yesterday"])
def test_requires_time_range_when_time_phrase_detected(classifier: QuestionClassifier, question: str) -> None:
    classification = classifier.classify(question)

    assert classification.requires_time_range is True


def test_normal_question_is_low_risk(classifier: QuestionClassifier) -> None:
    classification = classifier.classify("show paid GMV by channel")

    assert classification.risk_level == "low"
