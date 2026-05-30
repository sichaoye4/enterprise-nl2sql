from __future__ import annotations

from src.semantic_registry.pipeline.llm_gateway import (
    LLMGateway,
    LLMResponse,
    MockLLMProvider,
    TransientLLMError,
    validate_select_sql,
)


def generation_prompt() -> str:
    return """
<generation_context>
{
  "semantic_plan": {
    "metric": "paid_gmv",
    "dimension": "channel",
    "time_range": "last_month",
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
"""


def test_mock_provider_generate_returns_llm_response_with_valid_sql() -> None:
    response = MockLLMProvider().generate(generation_prompt())

    assert isinstance(response, LLMResponse)
    assert validate_select_sql(response.sql) == []


def test_llm_gateway_generate_with_mock_provider_returns_response() -> None:
    response = LLMGateway(provider=MockLLMProvider()).generate(generation_prompt())

    assert isinstance(response, LLMResponse)
    assert response.sql.startswith("SELECT")
    assert response.tables_used == ["orders"]


def test_llm_gateway_defaults_to_deepseek_when_api_key_is_set(monkeypatch) -> None:
    from src.semantic_registry.pipeline.llm_gateway import DeepSeekProvider

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    gateway = LLMGateway()

    assert isinstance(gateway.provider, DeepSeekProvider)


def test_mock_provider_generates_join_when_dimension_mapping_is_cross_table() -> None:
    prompt = """
<generation_context>
{
  "semantic_plan": {
    "metric": "paid_gmv",
    "dimension": "campaign",
    "time_range": null,
    "time_semantics": "payment_date",
    "domain": "commerce",
    "filters": []
  },
  "physical_mapping": {
    "table": "ads_order_channel_daily",
    "metric_column": "paid_gmv_amt",
    "metric_expression": "paid_gmv_amt",
    "dimension_table": "ads_campaign_daily",
    "dimension_column": "campaign_name",
    "time_column": "payment_dt",
    "aggregation": "sum"
  },
  "join_paths": [
    {
      "from_table": "ads_order_channel_daily",
      "to_table": "ads_campaign_daily",
      "relationship": "many_to_one",
      "join_condition": "ads_order_channel_daily.campaign_id = ads_campaign_daily.campaign_id",
      "safe_for_metrics": ["paid_gmv"],
      "fanout_risk": "low"
    }
  ],
  "candidate_tables": ["ads_order_channel_daily", "ads_campaign_daily"],
  "known_caveats": []
}
</generation_context>
"""

    response = MockLLMProvider().generate(prompt)

    assert "LEFT JOIN ads_campaign_daily j ON o.campaign_id = j.campaign_id" in response.sql
    assert "j.campaign_name AS campaign" in response.sql
    assert "SUM(o.paid_gmv_amt) AS paid_gmv" in response.sql
    assert validate_select_sql(response.sql) == []
    assert response.tables_used == ["ads_order_channel_daily", "ads_campaign_daily"]


def test_validate_select_sql_accepts_valid_select() -> None:
    errors = validate_select_sql("SELECT channel, SUM(paid_gmv_amt) AS paid_gmv FROM orders GROUP BY channel")

    assert errors == []


def test_validate_select_sql_rejects_insert() -> None:
    errors = validate_select_sql("INSERT INTO orders (id) VALUES (1)")

    assert "Only SELECT statements are allowed" in errors


def test_validate_select_sql_rejects_select_star() -> None:
    errors = validate_select_sql("SELECT * FROM orders")

    assert "SELECT * is not allowed" in errors


def test_llm_gateway_retries_on_transient_failures() -> None:
    class FlakyProvider:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, prompt: str) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                raise TransientLLMError("temporary failure")
            return MockLLMProvider().generate(prompt)

    provider = FlakyProvider()
    response = LLMGateway(provider=provider, retries=2).generate(generation_prompt())

    assert provider.calls == 2
    assert response.sql.startswith("SELECT")


def test_llm_response_fields_are_populated() -> None:
    response = MockLLMProvider().generate(generation_prompt())

    assert response.sql
    assert response.assumptions
    assert response.tables_used
    assert response.columns_used
    assert response.confidence in {"high", "medium", "low"}
    assert response.reasoning_summary
