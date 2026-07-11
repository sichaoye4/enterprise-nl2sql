from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.semantic_registry.pipeline.candidate_generator import SQLCandidate
from src.semantic_registry.pipeline.semantic_judge import (
    DashScopeLLMClient,
    JudgeResult,
    LLMJudge,
    build_judge_prompt,
    parse_judge_response,
)
from src.semantic_registry.pipeline.state_machine import NL2SQLPipeline
from tests.resolver.conftest import (  # noqa: F401
    registry_data as registry_data,
    resolver_concepts as resolver_concepts,
    resolver_dimensions as resolver_dimensions,
    resolver_metrics as resolver_metrics,
    resolver_terms as resolver_terms,
)


class FakeSemanticEngine:
    def __init__(self, result: dict) -> None:
        self.result = result

    def process(self, question: str):
        return self.result


class FakeJudgeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses[min(len(self.prompts) - 1, len(self.responses) - 1)]


class RetryCandidateGenerator:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str | None] = []

    def generate_candidates(self, context):
        self.calls += 1
        self.prompts.append(context.context_prompt)
        if self.calls == 1:
            sql = "SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders"
            reasoning = "First attempt."
        else:
            sql = "SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders WHERE payment_dt IS NOT NULL"
            reasoning = "Retry with time-safe filter."
        return [
            SQLCandidate(
                candidate_id=f"candidate_{self.calls}",
                sql=sql,
                generation_strategy="direct",
                assumptions=[],
                tables_used=["orders"],
                columns_used=["paid_gmv_amt"],
                confidence="high",
                reasoning_summary=reasoning,
                parse_success=True,
                validation_errors=[],
            )
        ]


def test_parse_judge_response_accepts_json_object_from_markdown() -> None:
    result = parse_judge_response(
        """
```json
{"pass": true, "reasoning": "SQL matches the requested metric.", "confidence": 0.91}
```
"""
    )

    assert result == JudgeResult(pass_=True, reasoning="SQL matches the requested metric.", confidence=0.91)


def test_llm_judge_accepts_and_rejects_sql_from_structured_response() -> None:
    accept_client = FakeJudgeClient(['{"pass": true, "reasoning": "answers the question", "confidence": 0.8}'])
    reject_client = FakeJudgeClient(['{"pass": false, "reasoning": "missing channel grouping", "confidence": 0.7}'])

    accepted = LLMJudge(client=accept_client).judge(
        "show paid GMV",
        "SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders",
        "LLM",
        None,
    )
    rejected = LLMJudge(client=reject_client).judge(
        "show paid GMV by channel",
        "SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders",
        "LLM",
        None,
    )

    assert accepted.pass_ is True
    assert rejected.pass_ is False
    assert "missing channel" in rejected.reasoning


def test_judge_prompt_includes_question_sql_route_and_plan() -> None:
    prompt = build_judge_prompt(
        "show paid GMV by channel",
        "SELECT channel, SUM(paid_gmv_amt) AS paid_gmv FROM orders GROUP BY channel",
        "GUARDED_LLM_SQL",
        {"metric": "paid_gmv", "dimension": "channel"},
    )

    assert "show paid GMV by channel" in prompt
    assert "GROUP BY channel" in prompt
    assert "GUARDED_LLM_SQL" in prompt
    assert '"metric": "paid_gmv"' in prompt


def test_dashscope_client_loads_config_file(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "api_key": "config-key",
                "base_url": "https://example.test/v1",
                "model": "qwen3.5-plus",
                "max_tokens": 123,
            }
        ),
        encoding="utf-8",
    )

    client = DashScopeLLMClient(config_path=config_path)

    assert client.api_key == "config-key"
    assert client.base_url == "https://example.test/v1"
    assert client.model == "qwen3.5-plus"
    assert client.max_tokens == 123


def test_dashscope_client_falls_back_to_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "env-key")

    client = DashScopeLLMClient(config_path=tmp_path / "missing.json")

    assert client.api_key == "env-key"


def test_llm_judge_retry_flow_injects_feedback_and_reselects(registry_data) -> None:
    generator = RetryCandidateGenerator()
    judge = LLMJudge(
        client=FakeJudgeClient(
            [
                '{"pass": false, "reasoning": "missing required freshness filter", "confidence": 0.82}',
                '{"pass": true, "reasoning": "answers the revised request", "confidence": 0.9}',
            ]
        )
    )
    pipeline = NL2SQLPipeline(
        registry_data=registry_data,
        semantic_engine=FakeSemanticEngine({"route": "BASELINE_LLM"}),
        candidate_generator=generator,
        llm_judge=judge,
    )

    context = pipeline.run("show paid GMV")

    assert context.llm_judge_retry_count == 1
    assert context.llm_judge_result is not None
    assert context.llm_judge_result["pass"] is True
    assert context.response is not None
    assert context.response.generated_sql == "SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders WHERE payment_dt IS NOT NULL"
    assert generator.calls == 2
    assert generator.prompts[1] is not None
    assert "[Previous Attempt Feedback]" in generator.prompts[1]
    assert "missing required freshness filter" in generator.prompts[1]


def test_semantic_sql_judge_failure_falls_back_to_semantic_assisted_generation(registry_data) -> None:
    generator = RetryCandidateGenerator()
    judge = LLMJudge(
        client=FakeJudgeClient(
            [
                '{"pass": false, "reasoning": "ambiguous semantic intent", "confidence": 0.6}',
                '{"pass": true, "reasoning": "assisted SQL answers the request", "confidence": 0.9}',
            ]
        )
    )
    pipeline = NL2SQLPipeline(
        registry_data=registry_data,
        semantic_engine=FakeSemanticEngine(
            {
                "route": "SEMANTIC_SQL",
                "compiled_query": {
                    "sql": "SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders",
                    "lineage": {
                        "tables": ["orders"],
                        "measures": {"paid_gmv": {"column": "paid_gmv_amt"}},
                    },
                },
            }
        ),
        candidate_generator=generator,
        llm_judge=judge,
    )

    context = pipeline.run("show paid GMV")

    assert context.llm_judge_retry_count == 1
    assert context.llm_judge_result is not None
    assert context.llm_judge_result["pass"] is True
    assert context.semantic_route == "SEMANTIC_ASSISTED_LLM"
    assert generator.calls == 2
    assert context.response is not None
    assert context.response.generated_sql.startswith("SELECT")


@pytest.mark.skipif(
    not (os.getenv("DASHSCOPE_API_KEY") or (Path.home() / ".hermes" / "skills" / "mlops" / "multimodal-vision" / "config.json").exists()),
    reason="DashScope API credentials are not configured.",
)
def test_dashscope_judge_real_api_smoke() -> None:
    result = LLMJudge().judge(
        "show paid GMV",
        "SELECT SUM(paid_gmv_amt) AS paid_gmv FROM orders",
        "LLM",
        {"metric": "paid_gmv"},
    )

    assert isinstance(result.pass_, bool)
    assert result.reasoning
    assert 0.0 <= result.confidence <= 1.0

