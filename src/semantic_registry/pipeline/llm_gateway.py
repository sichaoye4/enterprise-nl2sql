from __future__ import annotations

import json
import os
import re
from typing import Any, Protocol, runtime_checkable

import sqlglot
from sqlglot import exp

from src.semantic_registry.pipeline.llm_gateway_types import LLMResponse


class TransientLLMError(RuntimeError):
    pass


@runtime_checkable
class LLMProvider(Protocol):
    def generate(self, prompt: str) -> LLMResponse | str:
        ...


def validate_select_sql(sql: str) -> list[str]:
    errors: list[str] = []
    if not sql or not sql.strip():
        return ["SQL is empty"]
    try:
        statements = [statement for statement in sqlglot.parse(sql) if statement is not None]
    except sqlglot.errors.ParseError as exc:
        return [f"SQL parse error: {exc}"]
    if len(statements) != 1:
        errors.append("SQL must contain exactly one statement")
        return errors
    statement = statements[0]
    if not isinstance(statement, exp.Select):
        errors.append("Only SELECT statements are allowed")
    if any(True for _star in statement.find_all(exp.Star)):
        errors.append("SELECT * is not allowed")
    return errors


class MockLLMProvider:
    def generate(self, prompt: str) -> LLMResponse:
        context = self._extract_generation_context(prompt)
        sql = self._build_sql(context)
        return LLMResponse(
            sql=sql,
            assumptions=self._assumptions(context),
            tables_used=self._tables_used(context),
            columns_used=self._columns_used(context),
            confidence="high" if context.get("table") and context.get("metric_expression") else "medium",
            reasoning_summary="Generated deterministic SQL from the resolved semantic plan and physical mappings.",
        )

    def _extract_generation_context(self, prompt: str) -> dict:
        match = re.search(r"<generation_context>\s*(.*?)\s*</generation_context>", prompt, flags=re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return {}
        plan = data.get("semantic_plan") or {}
        mapping = data.get("physical_mapping") or {}
        join_paths = data.get("join_paths") or []
        return {
            "metric": plan.get("metric") or "metric_value",
            "dimension": plan.get("dimension"),
            "time_range": plan.get("time_range"),
            "time_semantics": plan.get("time_semantics"),
            "table": mapping.get("table") or "semantic_query_source",
            "metric_expression": mapping.get("metric_expression") or mapping.get("metric_column") or "metric_value",
            "metric_column": mapping.get("metric_column"),
            "dimension_column": mapping.get("dimension_column"),
            "dimension_table": mapping.get("dimension_table"),
            "time_column": mapping.get("time_column"),
            "aggregation": mapping.get("aggregation") or "sum",
            "join_paths": join_paths if isinstance(join_paths, list) else [],
        }

    def _build_sql(self, context: dict) -> str:
        metric_alias = self._identifier(context.get("metric") or "metric_value")
        table_name = context.get("table") or "semantic_query_source"
        table = self._relation(table_name)
        dimension_column = context.get("dimension_column")
        join_path = self._join_path(context)
        use_join = join_path is not None
        metric_expression = self._metric_expression(context, table_alias="o" if use_join else None)
        time_column = context.get("time_column")

        select_parts: list[str] = []
        group_parts: list[str] = []
        if dimension_column:
            dimension_identifier = self._qualified_identifier(dimension_column, "j" if use_join else None)
            dimension_alias = self._identifier(context.get("dimension") or dimension_column)
            select_parts.append(f"{dimension_identifier} AS {dimension_alias}")
            group_parts.append(dimension_identifier)
        select_parts.append(f"{metric_expression} AS {metric_alias}")

        sql = f"SELECT {', '.join(select_parts)} FROM {table}"
        if join_path is not None:
            join_table = self._relation(str(join_path["to_table"]))
            join_condition = self._aliased_join_condition(str(join_path["join_condition"]), table_name, join_path["to_table"])
            sql += f" o LEFT JOIN {join_table} j ON {join_condition}"
        where_clause = self._time_filter(time_column, context.get("time_range"), table_alias="o" if use_join else None)
        if where_clause is None and time_column and context.get("time_semantics"):
            where_clause = f"{self._qualified_identifier(time_column, 'o' if use_join else None)} IS NOT NULL"
        if where_clause:
            sql += f" WHERE {where_clause}"
        if group_parts:
            sql += f" GROUP BY {', '.join(group_parts)} ORDER BY {metric_alias} DESC"
        return sql

    def _metric_expression(self, context: dict, table_alias: str | None = None) -> str:
        expression = context.get("metric_expression") or context.get("metric_column") or "metric_value"
        metric_column = context.get("metric_column")
        aggregation = (context.get("aggregation") or "sum").lower()
        if metric_column and expression == metric_column and aggregation in {"sum", "avg", "min", "max", "count"}:
            return f"{aggregation.upper()}({self._qualified_identifier(metric_column, table_alias)})"
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(expression)):
            return self._qualified_identifier(str(expression), table_alias)
        return str(expression)

    def _time_filter(self, time_column: str | None, time_range: str | None, table_alias: str | None = None) -> str | None:
        if not time_column or not time_range:
            return None
        column = self._qualified_identifier(time_column, table_alias)
        ranges = {
            "today": f"{column} = CURRENT_DATE",
            "yesterday": f"{column} = CURRENT_DATE - 1",
            "last_month": f"{column} >= CURRENT_DATE - 31",
            "current_month": f"{column} >= CURRENT_DATE - 31",
            "last_quarter": f"{column} >= CURRENT_DATE - 92",
            "current_quarter": f"{column} >= CURRENT_DATE - 92",
        }
        if time_range in ranges:
            return ranges[time_range]
        match = re.fullmatch(r"last_(\d+)_days", time_range)
        if match:
            return f"{column} >= CURRENT_DATE - {match.group(1)}"
        return f"{column} IS NOT NULL"

    def _assumptions(self, context: dict) -> list[str]:
        assumptions = ["Only read-only SELECT SQL is generated."]
        if context.get("time_range"):
            assumptions.append(f"Time range interpreted as {context['time_range']}.")
        if context.get("dimension"):
            assumptions.append(f"Grouped by {context['dimension']}.")
        return assumptions

    def _columns_used(self, context: dict) -> list[str]:
        columns = [
            context.get("metric_column"),
            context.get("dimension_column"),
            context.get("time_column"),
        ]
        return [column for column in columns if column]

    def _tables_used(self, context: dict) -> list[str]:
        tables = [context["table"]] if context.get("table") else []
        join_path = self._join_path(context)
        if join_path and join_path.get("to_table"):
            tables.append(str(join_path["to_table"]))
        return list(dict.fromkeys(tables))

    def _join_path(self, context: dict) -> dict[str, Any] | None:
        join_paths = [join for join in context.get("join_paths", []) if isinstance(join, dict)]
        if not join_paths or not context.get("dimension_column"):
            return None
        dimension_table = context.get("dimension_table")
        source_table = context.get("table")
        for join in join_paths:
            if not join.get("to_table") or not join.get("join_condition"):
                continue
            if dimension_table and dimension_table != source_table and join.get("to_table") == dimension_table:
                return join
        return None

    def _identifier(self, value: str) -> str:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            return value
        return re.sub(r"[^A-Za-z0-9_]", "_", value).strip("_") or "value"

    def _qualified_identifier(self, value: str, table_alias: str | None = None) -> str:
        identifier = self._identifier(value)
        return f"{table_alias}.{identifier}" if table_alias else identifier

    def _relation(self, value: str) -> str:
        parts = value.split(".")
        return ".".join(self._identifier(part) for part in parts if part)

    def _aliased_join_condition(self, join_condition: str, from_table: str, to_table: str) -> str:
        replacements = {
            from_table: "o",
            to_table: "j",
            self._relation(from_table): "o",
            self._relation(to_table): "j",
        }
        condition = join_condition
        for table_name in sorted(replacements, key=len, reverse=True):
            condition = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(table_name)}\.", f"{replacements[table_name]}.", condition)
        return condition


MockProvider = MockLLMProvider


class DeepSeekProvider:
    """DeepSeek API provider via OpenAI-compatible endpoint."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        reasoning_effort: str = "high",
    ) -> None:
        self.model = model or os.getenv("LLM_MODEL", "deepseek-chat")
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        self.reasoning_effort = reasoning_effort
        self._client: Any | None = None

    def generate(self, prompt: str) -> str:
        system_prompt = (
            "Return JSON only with fields: sql, assumptions, tables_used, columns_used, "
            "confidence, reasoning_summary. Generate exactly one read-only SELECT statement. "
            "Do not use SELECT *. Do not generate write, DDL, or multi-statement SQL."
        )
        return self.generate_text(prompt, system_prompt=system_prompt)

    def generate_text(self, prompt: str, system_prompt: str | None = None) -> str:
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required to use DeepSeekProvider.")
        try:
            client = self._client or self._build_client()
            self._client = client
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt or "Return only valid JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                extra_body={"reasoning_effort": self.reasoning_effort},
            )
        except self._transient_errors() as exc:
            raise TransientLLMError(f"DeepSeek API transient error: {exc}") from exc
        return response.choices[0].message.content or ""

    def _build_client(self) -> Any:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("The openai package is required to use DeepSeekProvider. Install openai>=1.0.") from exc
        return OpenAI(base_url=self.base_url, api_key=self.api_key)

    def _transient_errors(self) -> tuple[type[BaseException], ...]:
        try:
            from openai import APIConnectionError, APITimeoutError, RateLimitError
        except ImportError:
            return (TimeoutError, ConnectionError)
        return (APITimeoutError, RateLimitError, APIConnectionError, TimeoutError, ConnectionError)


OpenAIProvider = DeepSeekProvider


class LLMGateway:
    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        model: str | None = None,
        retries: int = 2,
    ) -> None:
        self.provider = provider or (DeepSeekProvider(model=model) if os.getenv("DEEPSEEK_API_KEY") else MockLLMProvider())
        self.model = model or os.getenv("LLM_MODEL", "deepseek-chat")
        self.retries = retries

    def generate(self, prompt: str, system_prompt: str | None = None) -> LLMResponse:
        from src.semantic_registry.pipeline.json_parser import StrictJSONParser

        contracted_prompt = self._with_contract(prompt, system_prompt)
        parser = StrictJSONParser()
        last_error: Exception | None = None
        for _attempt in range(self.retries + 1):
            try:
                raw = self.provider.generate(contracted_prompt)
                response = raw if isinstance(raw, LLMResponse) else parser.parse(raw)
                self._validate_response(response)
                return response
            except (TransientLLMError, TimeoutError, ConnectionError, ValueError) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("LLM generation failed")

    def _with_contract(self, prompt: str, system_prompt: str | None) -> str:
        contract = (
            "Return JSON only with fields: sql, assumptions, tables_used, columns_used, "
            "confidence, reasoning_summary. Generate SELECT-only SQL. Do not use SELECT *."
        )
        parts = [system_prompt, contract, prompt]
        return "\n\n".join(part for part in parts if part)

    def _validate_response(self, response: LLMResponse) -> None:
        errors = validate_select_sql(response.sql)
        if errors:
            raise ValueError("; ".join(errors))
        valid_confidence = {"high", "medium", "low"}
        if response.confidence not in valid_confidence:
            raise ValueError(f"confidence must be one of {sorted(valid_confidence)}")

    def generate_text(self, prompt: str, system_prompt: str | None = None) -> str:
        provider_generate_text = getattr(self.provider, "generate_text", None)
        if callable(provider_generate_text):
            return provider_generate_text(prompt, system_prompt=system_prompt)
        raw = self.provider.generate(self._with_contract(prompt, system_prompt))
        if isinstance(raw, str):
            return raw
        if hasattr(raw, "model_dump_json"):
            return raw.model_dump_json()
        return json.dumps(raw)


__all__ = [
    "LLMGateway",
    "LLMProvider",
    "LLMResponse",
    "DeepSeekProvider",
    "MockLLMProvider",
    "MockProvider",
    "OpenAIProvider",
    "TransientLLMError",
    "validate_select_sql",
]
