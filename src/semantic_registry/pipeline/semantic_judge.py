from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_DASHSCOPE_MODEL = "qwen3.5-plus"
DEFAULT_DASHSCOPE_CONFIG_PATH = Path.home() / ".hermes" / "skills" / "mlops" / "multimodal-vision" / "config.json"


@dataclass(frozen=True)
class JudgeResult:
    pass_: bool
    reasoning: str
    confidence: float


class DashScopeLLMClient:
    """OpenAI-compatible DashScope client used by the cross-model SQL judge."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        config = self._load_config(Path(config_path) if config_path is not None else DEFAULT_DASHSCOPE_CONFIG_PATH)
        self.api_key = api_key or config.get("api_key") or os.getenv("DASHSCOPE_API_KEY")
        self.base_url = base_url or config.get("base_url") or os.getenv("DASHSCOPE_BASE_URL", DEFAULT_DASHSCOPE_BASE_URL)
        self.model = model or config.get("model") or os.getenv("DASHSCOPE_MODEL", DEFAULT_DASHSCOPE_MODEL)
        self.max_tokens = int(max_tokens or config.get("max_tokens") or 4096)
        self._client: Any | None = None

    def generate(self, prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY or DashScope config api_key is required to use the LLM judge.")
        client = self._client or self._build_client()
        self._client = client
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an independent SQL semantic judge. Return only JSON with keys: "
                        "pass, reasoning, confidence."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=self.max_tokens,
        )
        return response.choices[0].message.content or ""

    def _build_client(self) -> Any:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("The openai package is required to use DashScopeLLMClient. Install openai>=1.0.") from exc
        return OpenAI(base_url=self.base_url, api_key=self.api_key)

    def _load_config(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


def build_judge_prompt(question: str, sql: str, route_type: str | None, semantic_plan: Any | None) -> str:
    plan = _jsonable(semantic_plan)
    payload = {
        "question": question,
        "generated_sql": sql,
        "route_type": route_type or "LLM",
        "semantic_plan": plan,
    }
    return "\n".join(
        [
            "Decide whether the generated SQL semantically answers the user's question.",
            "Check metric choice, filters, dimensions/grouping, time semantics, aggregation, and obvious mismatches.",
            "Do not reject for harmless formatting differences.",
            "Return JSON only in this shape:",
            '{"pass": true, "reasoning": "brief explanation", "confidence": 0.0}',
            "",
            "<judge_input>",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "</judge_input>",
        ]
    )


def parse_judge_response(text: str) -> JudgeResult:
    data = _extract_json_object(text)
    passed = bool(data.get("pass"))
    reasoning = str(data.get("reasoning") or "").strip()
    confidence = _confidence(data.get("confidence"))
    return JudgeResult(pass_=passed, reasoning=reasoning, confidence=confidence)


class LLMJudge:
    def __init__(self, client: DashScopeLLMClient | None = None) -> None:
        self.client = client or DashScopeLLMClient()

    def judge(self, question: str, sql: str, route_type: str | None, semantic_plan: Any | None) -> JudgeResult:
        prompt = build_judge_prompt(question, sql, route_type, semantic_plan)
        return parse_judge_response(self.client.generate(prompt))


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool, list, tuple, dict)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if hasattr(value, "__dict__"):
        return {key: _jsonable(item) for key, item in value.__dict__.items() if not key.startswith("_")}
    return str(value)


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = json.loads(_first_json_object(cleaned))
    if not isinstance(data, dict):
        raise ValueError("Judge response must be a JSON object.")
    return data


def _first_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        raise ValueError("Judge response did not contain a JSON object.")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise ValueError("Judge response contained an incomplete JSON object.")


def _confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))
