from __future__ import annotations

from src.semantic_registry.evaluation.models import EvalCase, EvalResult
from src.semantic_registry.evaluation.runner import EvalRunner
from src.semantic_registry.pipeline import NL2SQLPipeline
from tests.resolver.conftest import (  # noqa: F401
    registry_data as registry_data,
    resolver_concepts as resolver_concepts,
    resolver_dimensions as resolver_dimensions,
    resolver_metrics as resolver_metrics,
    resolver_terms as resolver_terms,
)


def matching_case(gold_sql: str) -> EvalCase:
    return EvalCase(
        case_id="paid_gmv",
        question="show paid GMV",
        domain="commerce",
        difficulty="easy",
        expected_semantic_plan={
            "metric": "paid_gmv",
            "dimension": None,
            "time_range": None,
            "time_semantics": "payment_date",
            "domain": "commerce",
            "filters": [],
            "requires_clarification": False,
        },
        gold_sql=gold_sql,
        required_tables=["ads_order_channel_daily"],
        required_columns=["paid_gmv_amt"],
        active=True,
        tags=[],
    )


def test_run_semantic_eval_returns_eval_result_with_correct_structure(registry_data) -> None:
    pipeline = NL2SQLPipeline(registry_data=registry_data)
    case = matching_case(
        "SELECT SUM(t0.paid_gmv_amt) AS paid_gmv FROM ads_order_channel_daily AS t0"
    )

    result = EvalRunner().run_semantic_eval([case], pipeline)

    assert isinstance(result, EvalResult)
    assert result.total_cases == 1
    assert set(result.metrics) == {
        "term_extraction_accuracy",
        "concept_resolution_accuracy",
        "ambiguity_detection_accuracy",
    }
    assert result.case_results[0].generated_plan is not None


def test_run_sql_eval_passes_for_matching_sql(registry_data) -> None:
    pipeline = NL2SQLPipeline(registry_data=registry_data)
    case = matching_case(
        "SELECT SUM(t0.paid_gmv_amt) AS paid_gmv FROM ads_order_channel_daily AS t0"
    )

    result = EvalRunner().run_sql_eval([case], pipeline)

    assert result.passed == 1
    assert result.failed == 0


def test_run_sql_eval_fails_for_non_matching_sql(registry_data) -> None:
    pipeline = NL2SQLPipeline(registry_data=registry_data)
    case = matching_case("SELECT COUNT(order_id) AS order_count FROM orders")

    result = EvalRunner().run_sql_eval([case], pipeline)

    assert result.passed == 0
    assert result.failed == 1
