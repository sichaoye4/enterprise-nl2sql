from __future__ import annotations

from src.semantic_registry.pipeline.candidate_generator import SQLCandidate
from src.semantic_registry.pipeline.explainer import SQLExplainer
from src.semantic_registry.resolver.plan import SemanticQueryPlan


def test_explanation_uses_business_name_not_physical_name() -> None:
    explanation = SQLExplainer().explain(
        SemanticQueryPlan(metric="paid_gmv", dimension="channel", domain="commerce"),
        sql_candidate(),
    )

    assert explanation.metric_used == "Paid GMV"
    assert "paid_gmv_amt" not in explanation.metric_used


def test_explanation_includes_time_range_when_present() -> None:
    explanation = SQLExplainer().explain(
        SemanticQueryPlan(metric="paid_gmv", time_range="last_month", time_semantics="payment_date"),
        sql_candidate(),
    )

    assert explanation.time_range == "last_month"
    assert explanation.time_semantics == "Payment Date"


def test_explanation_includes_metric_reason_text() -> None:
    explanation = SQLExplainer().explain(
        SemanticQueryPlan(metric="paid_gmv", domain="commerce"),
        sql_candidate(),
    )

    assert "Paid GMV" in explanation.metric_reason
    assert "commerce" in explanation.metric_reason


def sql_candidate() -> SQLCandidate:
    return SQLCandidate(
        candidate_id="A",
        sql="SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders",
        generation_strategy="direct",
        assumptions=["Only read-only SELECT SQL is generated."],
        tables_used=["orders"],
        columns_used=["paid_gmv_amt"],
        confidence="high",
        reasoning_summary="Generated from test context.",
        parse_success=True,
        validation_errors=[],
    )
