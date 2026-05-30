from __future__ import annotations

from src.semantic_registry.pipeline.candidate_generator import CandidateGenerator
from src.semantic_registry.pipeline.state_machine import PipelineContext
from src.semantic_registry.resolver.plan import SemanticQueryPlan


def test_generate_candidates_returns_two_valid_strategies() -> None:
    context = PipelineContext(
        question="show paid GMV by channel",
        semantic_plan=SemanticQueryPlan(
            metric="paid_gmv",
            dimension="channel",
            time_semantics="payment_date",
            domain="commerce",
        ),
        context_prompt="""
<generation_context>
{
  "semantic_plan": {
    "metric": "paid_gmv",
    "dimension": "channel",
    "time_range": null,
    "time_semantics": "payment_date",
    "domain": "commerce",
    "filters": []
  },
  "physical_mapping": {
    "table": "orders",
    "metric_column": "paid_gmv_amt",
    "metric_expression": "paid_gmv_amt",
    "dimension_column": "channel",
    "time_column": "payment_dt",
    "aggregation": "sum"
  },
  "candidate_tables": ["orders"],
  "known_caveats": []
}
</generation_context>
""",
    )

    candidates = CandidateGenerator().generate_candidates(context)

    assert len(candidates) == 2
    assert candidates[0].generation_strategy == "direct"
    assert candidates[1].generation_strategy == "plan_first"
    assert all(candidate.parse_success for candidate in candidates)
    assert all(candidate.sql.startswith("SELECT") for candidate in candidates)
